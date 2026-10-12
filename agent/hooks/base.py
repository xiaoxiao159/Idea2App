from __future__ import annotations

from typing import Any, Callable

# (query) -> None 原样 / str 改写后
PromptHook = Callable[[str], str | None]

# (tool_name, params) -> None 默许 / str 否决理由
PreToolUseHook = Callable[[str, dict[str, Any]], str | None]

# (tool_name, params, result, is_error) -> 只观察,返回丢弃
PostToolUseHook = Callable[[str, dict[str, Any], str, bool], None]

# (answer, task_usage) -> None 允许收工 / str 驳回理由
StopHook = Callable[[str, dict[str, int]], str | None]
