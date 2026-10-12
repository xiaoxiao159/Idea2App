from __future__ import annotations

from agent.config import Config, ConfigError, sanitize_ssl_environment
from agent.models.base import Model


def get_model(config: Config) -> Model:
    """配置 → 模型实例:全项目唯一知道"存在哪几种协议"的地方。

    import 写在分支里:anthropic.py / openai.py 只在选中的那条路上加载,
    没装另一个 SDK 的环境照样能跑——依赖按需存在。
    """
    sanitize_ssl_environment()  # 构造 SDK client 前先清掉幽灵 CA 路径(conda/Windows)
    if config.provider == "anthropic":
        from agent.models.anthropic import AnthropicModel

        return AnthropicModel(config)
    if config.provider == "openai":
        from agent.models.openai import OpenAIModel

        return OpenAIModel(config)
    raise ConfigError(
        f"Unknown MODEL_PROVIDER: {config.provider!r}. "
        "Check MODEL_PROVIDER in .env (expected 'anthropic' or 'openai')."
    )
