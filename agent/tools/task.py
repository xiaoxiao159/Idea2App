"""task 工具:委派一个子 agent 去处理独立子任务。

schema 给父模型看,让父循环能把任务交给子 agent;handler 调 subagent.handle_task。
惰性导入是为了拆开 tools↔subagent 的 import 环——handler 只在运行期被调,那时模块已齐。
model 不进 handler 参数,而是 main 启动时 subagent.set_model 挂到模块级的状态。
"""
from __future__ import annotations

from typing import Any

from agent.tools.base import Tool


def _run_task(params: dict[str, Any]) -> tuple[str, bool]:
    from agent.subagent import handle_task  # 惰性导入:拆 import 环
    return handle_task(params)


def _display(params: dict[str, Any]) -> str:
    prompt = str(params.get("prompt", ""))
    prompt = " ".join(prompt.split())[:64]
    return f"task {prompt}"


TASK_TOOL = Tool(
    schema={
        "name": "task",
        "description": (
            "Run a subagent with a fresh, isolated conversation to complete a "
            "focused, self-contained subtask, then return only its final text. "
            "Use this for exploration, research, or any chunk of work whose "
            "intermediate tool calls would otherwise clutter the main context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "the self-contained task for the subagent",
                },
            },
            "required": ["prompt"],
        },
    },
    handler=_run_task,
    display=_display,
)