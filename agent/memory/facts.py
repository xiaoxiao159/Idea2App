"""事实记忆:跨会话成立的稳定事实,落成可编辑的 markdown。

三个文件(层级照 skills):
  用户级 ~/.idea2app/memory/profile.md    你的偏好/风格/约束
  项目级  .idea2app/memory/project.md     本项目决策/约定/状态/进行中目标
  项目级  .idea2app/memory/entities.md    命名对象(文件/函数/服务)及其属性

启动时全文注入(system),退出时 consolidate 把本次会话的稳固结论沉淀进 project.md。
markdown 而非 JSON:人可以直接打开改/删 —— 记忆该被人看得见、纠得正。

实体自动抽取暂缺:先靠人手写 entities.md;从 read/edit 调用自动 upsert 实体是后续会话的事。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

PROFILE, PROJECT, ENTITIES = "profile.md", "project.md", "entities.md"

CONSOLIDATE_SYSTEM = (
    "你是记忆沉淀器。从下面这段会话里,挑出值得跨会话记住的稳固事实,用要点列出:"
    "用户偏好/风格/约束、本项目的决定与理由、约定、进行中的目标。"
    "只写稳固结论,不写过程、不写一次性的执行细节。没有值得记的就只回 '(无)'。"
    "用中文,每行一条,简洁。"
)
CONSOLIDATE_MAX_CHARS = 30_000
CONSOLIDATE_MAX_TOKENS = 1_000


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def inject(user_mem: Path, project_mem: Path) -> str:
    """把常驻事实拼成一段进 system prompt。空则 ''。"""
    parts: list[str] = []
    p = _read(user_mem / PROFILE)
    if p:
        parts.append(f"## 关于用户的长期记忆\n{p}")
    j = _read(project_mem / PROJECT)
    if j:
        parts.append(f"## 本项目的长期记忆\n{j}")
    e = _read(project_mem / ENTITIES)
    if e:
        parts.append(f"## 本项目实体(命名对象及其属性)\n{e}")
    return "\n\n".join(parts)


def append_notes(project_mem: Path, text: str) -> None:
    """把一段沉淀追加进 project.md(带时间戳),只加不覆盖人手写的内容。"""
    project_mem.mkdir(parents=True, exist_ok=True)
    block = f"\n<!-- {datetime.now():%Y-%m-%d %H:%M} 自动沉淀 -->\n{text.strip()}\n"
    with (project_mem / PROJECT).open("a", encoding="utf-8", newline="\n") as f:
        f.write(block)


def consolidate(model: Any, transcript: str) -> str | None:
    """从会话原文抽稳固事实。取尾部(最近的才最相关);失败/空 → None。

    绝不因记忆这事中断主流程 —— 崩了静默返回 None,交给上层按 no-op 走。
    """
    if model is None or not transcript.strip():
        return None
    try:
        resp = model.complete(
            [{"role": "user", "content": transcript[-CONSOLIDATE_MAX_CHARS:]}],
            CONSOLIDATE_SYSTEM,
            [],
            CONSOLIDATE_MAX_TOKENS,
        )
    except Exception:
        return None
    text = "\n".join(
        b.get("text", "") for b in (resp.get("content") or []) if b.get("type") == "text"
    ).strip()
    if not text or text == "(无)":
        return None
    return text