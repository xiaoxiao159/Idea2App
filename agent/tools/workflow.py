"""workflow 工具:Workflow(schema + handler + display)。

薄壳:引擎在 agent/workflow.py。handler 同步桥接 asyncio 的 workflow runtime
(run_workflow_sync),把整次编排当作一次工具调用返回 JSON 结果。runner 由
main 启动时 workflow.set_runner(model) 换成真实模型。
"""
from __future__ import annotations

from typing import Any

from agent import workflow
from agent.tools.base import Tool


def _run(params: dict[str, Any]) -> tuple[str, bool]:
    name = str(params.get("name", "")).strip()
    args = params.get("args")
    resume = params.get("resume_from_run_id")
    text = workflow.run_workflow_sync(name, args=args, resume_from_run_id=resume)
    is_error = text.startswith("Error:")
    return (text, is_error)


def _display(params: dict[str, Any]) -> str:
    return f"workflow {params.get('name', '?')}"


WORKFLOW_TOOL = Tool(
    schema={
        "name": "Workflow",
        "description": (
            "Run a saved workflow by name. Pass input in args (e.g. "
            "'review-changes' with args.changes). Optionally resume a prior run "
            "with resume_from_run_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "args": {"type": "object"},
                "resume_from_run_id": {"type": "string"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    handler=_run,
    display=_display,
)