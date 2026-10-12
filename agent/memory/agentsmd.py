"""约定文件 AGENTS.md:人写的、跨会话常驻的偏好与约定,启动时拼进 system prompt。

两级,照 skills 的老规矩:先用户级、后项目级(更具体靠后)。
与 memory/ 里的 profile/project 分工清晰:
  AGENTS.md = 人写"我要你一直知道什么"(指令);
  memory/*  = agent 自己蒸馏"我从会话里学到什么"(事实)。
"""
from __future__ import annotations

from pathlib import Path

NAME = "AGENTS.md"


def load(user_dir: Path, project_dir: Path) -> str:
    """读两级 AGENTS.md,拼成一段文本;都没有 → ""(调用方无需 if)。

    utf-8-sig 剥 BOM(中文 Windows 记事本爱加);读不进/编码坏 → 跳过,不炸启动。
    """
    parts: list[str] = []
    for label, path in (("user", user_dir / NAME), ("project", project_dir / NAME)):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            parts.append(f"## ({label}) 长期约定\n{text}")
    return "\n\n".join(parts)