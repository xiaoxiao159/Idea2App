"""skill 安装器:市场清单(source=名) + 本地路径/URL(source=目录或 git 地址)。

一个 skill = 一个含 SKILL.md 的目录(agent/skills.py 的格式)。安装 = 解析来源 → 校验 → 落进技能库。
校验复用 agent 自己的 _parse_skill_md,不另造一套规则,也绝不放一个坏 skill 进库。

来源解析:
  - source 是市场清单里的名字 → 查 market.json(name → {source, desc})
  - 否则当成本地目录 / git URL(http(s)/git@/git:// 前缀判 git,其余判路径)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from Idea2App.settings import Settings


class InstallError(RuntimeError):
    """装不进 / skill 非法。"""


def load_market(market_file: Path) -> dict:
    if not market_file.is_file():
        return {}
    try:
        raw = json.loads(market_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"market.json unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise InstallError("market.json must be a JSON object")
    return raw


def install_skill(source: str, settings: Settings) -> str:
    """装一个 skill,返回其 name。source 是市场名、本地目录或 git URL。"""
    source = source.strip()
    if not source:
        raise InstallError("empty source")

    market = load_market(settings.market_file)
    if source in market:
        entry = market[source]
        src = entry.get("source") if isinstance(entry, dict) else entry
        if not src:
            raise InstallError(f"market entry '{source}' has no 'source'")
    else:
        src = source

    skill_dir = _resolve_src(str(src))
    name = _validate(skill_dir)
    _install(settings.skills_dir, skill_dir, name)
    return name


def _resolve_src(src: str) -> Path:
    if src.startswith(("http://", "https://", "git@", "git://")):
        return _clone(src)
    p = Path(src)
    if not p.is_dir():
        raise InstallError(f"not a directory: {src}")
    return _find_skill_dir(p)


def _clone(url: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="idea2app-skill-"))
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(tmp)],
            check=True, capture_output=True, text=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise InstallError(f"git clone failed: {exc}") from exc
    return _find_skill_dir(tmp)


def _find_skill_dir(root: Path) -> Path:
    """在 root 里找含 SKILL.md 的目录:root 自己,或离 root 最近的子目录。"""
    if (root / "SKILL.md").is_file():
        return root
    matches = [p.parent for p in root.rglob("SKILL.md") if p.is_file()]
    if not matches:
        raise InstallError(f"no SKILL.md found under {root}")
    # 取最浅的,避免把 skill 内部引用的示例 SKILL.md 误当主目录
    return min(matches, key=lambda d: len(d.relative_to(root).parts))


def _validate(skill_dir: Path) -> str:
    from agent.skills import _parse_skill_md  # 复用 agent 的校验规则,不另造

    skill = _parse_skill_md(skill_dir / "SKILL.md")
    if skill is None:
        raise InstallError(f"'{skill_dir}' is not a valid skill (SKILL.md invalid)")
    return skill.name


def _install(skills_dir: Path, skill_dir: Path, name: str) -> None:
    skills_dir.mkdir(parents=True, exist_ok=True)
    target = skills_dir / name
    if target.exists():
        print(f"  [install] overwriting existing skill '{name}'")
        shutil.rmtree(target)
    shutil.copytree(skill_dir, target)