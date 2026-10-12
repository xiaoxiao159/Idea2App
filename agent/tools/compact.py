"""compact 工具:schema + handler + display。

handler 只回一句确认——真正的压缩不在这里,而在 loop(整批工具跑完、回合闭合后
由 loop 调 compact.force)。原因和参考实现一致:一次响应可能同时"写文件 + 喊压缩",
必须先跑完整批、每个 tool_use 都有 tool_result,再压这个已经闭合的回合,才不丢副作用。
"""
from __future__ import annotations

from typing import Any

from agent.tools.base import Tool


def _run_compact(params: dict[str, Any]) -> tuple[str, bool]:
    return ("Compaction requested; the harness will archive completed turns after this batch.", False)


def _display(params: dict[str, Any]) -> str:
    return "compact"


COMPACT_TOOL = Tool(
    schema={
        "name": "compact",
        "description": "Summarize earlier conversation to free context space.",
        "input_schema": {"type": "object", "properties": {}},
    },
    handler=_run_compact,
    display=_display,
)