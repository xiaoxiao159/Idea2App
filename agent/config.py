from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from dotenv import load_dotenv

# 项目根 = 本文件(agent/config.py)的上一级。配置装载要确定性。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConfigError(RuntimeError):
    """配置缺失或非法。

    窄口子异常:继承方向 ConfigError ⊂ RuntimeError。
    调用方可以 `except ConfigError` 精确只捞配置错误,
    让别的错误继续向上炸——异常类型是给人留的"精确捞"的口子。
    """


@dataclass(frozen=True)
class _ProviderSpec:
    """一个 provider 的装载策略:必需哪些环境变量。

    新接协议 = 这里登记一行 + models/ 下加一个 adapter,
    本文件其余代码不动——登记表驱动,不是 if/else 遍布全文件。
    """

    required: tuple[str, ...]
    window: int  # 该 provider 默认上下文上限;env 的 CONTEXT_WINDOW 可覆盖


_PROVIDERS: dict[str, _ProviderSpec] = {
    # anthropic 维持 s01 行为:base_url 也必填(项目本来就跑在网关上)
    "anthropic": _ProviderSpec(("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"), window=200_000),
    # OpenAI 兼容协议(GPT / DeepSeek / Qwen 同款):
    # base_url 可空 → SDK 默认官方端点;换网关/换厂商在这里换 URL
    "openai": _ProviderSpec(("OPENAI_API_KEY",), window=128_000),
}


@dataclass(frozen=True)
class Config:
    """
    config 不该认识任何 SDK。SDK 对象由各 provider adapter 。
    """

    provider: str  # anthropic | openai(来自 MODEL_PROVIDER)
    model: str     # MODEL_ID:provider 语义下的模型名
    api_key: str   # {PROVIDER}_API_KEY
    base_url: str  # {PROVIDER}_BASE_URL;空 → 用 SDK 默认端点
    window: int = 200_000   # 上下文上限;默认 = anthropic 值(默认 provider 也恰是它)
    max_tokens: int = 8000  # 单次输出预留;预算与 complete 共用这一处,别各写各的(全项目对齐 8000)


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    """可选整数环境变量:空/缺 → default;给了但不是正整数 → ConfigError(显式设错要炸)。"""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


def build_config(env: Mapping[str, str]) -> Config:
    provider = (env.get("MODEL_PROVIDER") or "anthropic").strip().lower()
    if provider not in _PROVIDERS:
        raise ConfigError(
            f"Unknown MODEL_PROVIDER: {provider!r}. "
            f"Expected one of: {', '.join(sorted(_PROVIDERS))}."
        )

    prefix = provider.upper()  # anthropic → ANTHROPIC_*,openai → OPENAI_*
    spec = _PROVIDERS[provider]
    missing = [
        name
        for name in spec.required + ("MODEL_ID",)
        if not (env.get(name) or "").strip()
    ]
    if missing:
        raise ConfigError(
            f"Missing required environment variables for provider {provider!r}: "
            f"{', '.join(missing)}. Check .env at the project root."
        )

    return Config(
        provider=provider,
        model=(env.get("MODEL_ID") or "").strip(),
        api_key=(env.get(f"{prefix}_API_KEY") or "").strip(),
        base_url=(env.get(f"{prefix}_BASE_URL") or "").strip(),
        window=_env_int(env, "CONTEXT_WINDOW", spec.window),
        max_tokens=_env_int(env, "MAX_TOKENS", 8000),
    )


def load_config() -> Config:
    """读 .env → build_config。副作用只此一处,启动期调用一次。"""
    load_dotenv(os.path.join(_ROOT, ".env"), override=True)
    return build_config(os.environ)


def sanitize_ssl_environment() -> None:
    """摘掉指向不存在文件的 SSL_CERT_FILE,让 SDK 回落到默认信任库。

    conda 的 openssl_activate.sh 在 Git Bash 下会把 SSL_CERT_FILE 指向 Unix 路径
    `${CONDA_PREFIX}/ssl/cacert.pem`,而 Windows 布局的真实证书在 `Library/ssl/`。
    这个幽灵路径会让 anthropic/openai 构造 HTTP client 时直接在 ssl 层崩(
    FileNotFoundError)。指向不存在文件的环境变量不该硬炸掉整个 agent,
    这里统一在构造 SDK client 前把它清掉,幂等、只删坏值不动好值。
    """
    cert = os.environ.get("SSL_CERT_FILE")
    if cert and not os.path.isfile(cert):
        os.environ.pop("SSL_CERT_FILE", None)
