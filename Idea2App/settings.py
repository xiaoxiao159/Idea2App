"""Idea2App 自己的配置:技能库与市场清单的位置。

只认环境变量 + 默认值,不搞第二份配置文件——一行 env 就能换位置,
也不抢 agent/config.py 的 .env(引擎归引擎,流程归流程)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 用户技能库默认位置(用户指定)。install 落这里,load_skills 从这里装载。
DEFAULT_SKILLS_DIR = r"D:\AI\Job_Resume\.claude\skills"


@dataclass(frozen=True)
class Settings:
    skills_dir: Path          # 用户技能库(SKILL.md 目录们)
    market_file: Path         # 市场清单:name -> {source, desc}
    project_dir: Path         # 项目根:PRD.md / SPEC.md / .idea2app(含 phase.json)落这里
    project_skills_dir: Path  # 项目级技能库(仓库内 .idea2app/skills,留给本项目专属 skill)

    @classmethod
    def from_env(cls) -> "Settings":
        skills_dir = Path(os.environ.get("IDEA2APP_SKILLS_DIR") or DEFAULT_SKILLS_DIR)
        project_dir = Path.cwd()
        market_file = Path(os.environ.get("IDEA2APP_MARKET") or (skills_dir / "market.json"))
        return cls(
            skills_dir=skills_dir,
            market_file=market_file,
            project_dir=project_dir,
            project_skills_dir=project_dir / ".idea2app" / "skills",
        )