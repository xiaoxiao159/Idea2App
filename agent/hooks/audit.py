from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from agent import tools

LOG = Path("logs") / "session.log"


def _append(line: str) -> None:
    """mkdir -p + 追加一行。崩溃由 fire_* 层统一兜住:故障语义归触发层,不搞双保险。"""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def _oneline(text: str, limit: int) -> str:
    """压成一行:换行/制表符拍平,防一条日志撕成 N 行;再截断限长。"""
    return " ".join(text.split())[:limit]


def on_prompt(query: str) -> str | None:
    _append(f"{datetime.now():%H:%M:%S}\tquery\t{_oneline(query, 200)}")
    return None


def on_tool(name: str, params: dict[str, Any], result: str, is_error: bool) -> None:
    _append(
        f"{datetime.now():%H:%M:%S}\t{'ERR' if is_error else 'ok'}\t"
        f"{_oneline(tools.summary(name, params), 160)}\t{_oneline(result, 120)}"
    )


def on_stop(answer: str, usage: dict[str, int]) -> str | None:
    _append(
        f"{datetime.now():%H:%M:%S}\tstop\tin={usage.get('input', 0)} "
        f"out={usage.get('output', 0)}\t{_oneline(answer, 80)}"
    )
    return None
