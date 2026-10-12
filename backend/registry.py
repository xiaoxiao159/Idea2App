"""supervisor 的配置与项目注册表(默认 ~/.idea2app/,IDEA2APP_DIR 可覆盖)。

- ConfigStore:全局 API 配置 config.json;首次启动从 agent .env 种子迁移(不强迫重输)。
- ProjectStore:项目列表 projects.json。
- to_env(cfg):config dict → worker 子进程环境变量映射(字段名逐字对齐 agent/config.py)。
- pick_port():绑 127.0.0.1:0 让 OS 给临时端口。
- MODEL_ENV_KEYS:spawn worker 时要从继承环境里删掉的 model 键,防止 .env/残留值串味。

本模块 import agent.config(纯函数,无 import 期副作用),不 import agent 的重状态模块。
"""
from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path

from agent.config import load_config  # 仅在种子迁移时调用(会 load_dotenv)

HOME = Path(os.environ.get("IDEA2APP_DIR") or (Path.home() / ".idea2app"))
CONFIG_PATH = HOME / "config.json"
PROJECTS_PATH = HOME / "projects.json"

# 与 agent/config.py 的 build_config 逐一对应;anthropic 的 base_url 必填。
MODEL_ENV_KEYS = (
    "MODEL_PROVIDER", "MODEL_ID", "MAX_TOKENS", "CONTEXT_WINDOW",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_BASE_URL",
)


def to_env(cfg: dict) -> dict[str, str]:
    """config dict → worker 环境变量。字段名与 agent/config.py::build_config 一致。"""
    provider = (str(cfg.get("provider") or "anthropic")).strip().lower()
    prefix = provider.upper()
    return {
        "MODEL_PROVIDER": provider,
        "MODEL_ID": str(cfg.get("model") or ""),
        f"{prefix}_API_KEY": str(cfg.get("api_key") or ""),
        f"{prefix}_BASE_URL": str(cfg.get("base_url") or ""),
        "MAX_TOKENS": str(cfg.get("max_tokens") or 8000),
        "CONTEXT_WINDOW": str(cfg.get("window") or 200000),
    }


def pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def last_stage_of(directory: str) -> str | None:
    """只读 <项目>/.idea2app/phase.json 的阶段,不 import agent、不拉 worker。"""
    p = Path(directory) / ".idea2app" / "phase.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("stage")
    except (OSError, json.JSONDecodeError):
        return None


def new_project(name: str, directory: str) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "dir": str(Path(directory).resolve()),
        "created": time.time(),
        "status": "stopped",
        "last_stage": None,
        "port": None,
    }


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else CONFIG_PATH

    def load(self) -> dict:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return self._normalize(data)
            except (OSError, json.JSONDecodeError):
                pass
        seed = self._seed()
        self.save(seed)
        return seed

    def _seed(self) -> dict:
        """从 .env 迁移一份现有配置(区分于"空默认")。失败则退空默认。"""
        try:
            cfg = load_config()
            return {
                "provider": cfg.provider,
                "api_key": cfg.api_key,
                "base_url": cfg.base_url,
                "model": cfg.model,
                "max_tokens": cfg.max_tokens,
                "window": cfg.window,
            }
        except Exception:
            return {
                "provider": "anthropic", "api_key": "", "base_url": "",
                "model": "", "max_tokens": 8000, "window": 200000,
            }

    def _normalize(self, data: dict) -> dict:
        base = self._empty()
        base.update({k: data[k] for k in base if k in data})
        return base

    def _empty(self) -> dict:
        return {"provider": "anthropic", "api_key": "", "base_url": "",
                "model": "", "max_tokens": 8000, "window": 200000}

    def save(self, cfg: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._normalize(cfg)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


class ProjectStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else PROJECTS_PATH

    def load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def save(self, projects: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 剥离运行时内部字段(_proc/_logf 等)再落盘
        clean = [{k: v for k, v in p.items() if not str(k).startswith("_")} for p in projects]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)