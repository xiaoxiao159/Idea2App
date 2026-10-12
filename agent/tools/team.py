"""团队工具:Lead 侧的 7 个 Tool(spawn / list / send / shutdown / plan / review / worktree)。

薄壳:运行时与协议在 agent/team.py。handler 只做参数转译,把 team 模块的返回
捏成 (text, is_error)。spawn 成功后队友线程在后台跑,本轮结束由 main 在回合
边界投递 result / idle_notification 事件。
"""
from __future__ import annotations

from typing import Any

from agent import team
from agent.tools.base import Tool


def _spawn(params: dict[str, Any]) -> tuple[str, bool]:
    name = str(params.get("name", "")).strip()
    role = str(params.get("role", "teammate")).strip()
    prompt = str(params.get("prompt", "")).strip()
    task_id = params.get("task_id") or None
    require_plan = bool(params.get("require_plan", False))
    text = team.run_spawn_teammate(name, role, prompt, task_id, require_plan)
    return (text, text.startswith(("Invalid", "Cannot spawn")))


def _list_teammates(params: dict[str, Any]) -> tuple[str, bool]:
    return (team.run_list_teammates(), False)


def _send(params: dict[str, Any]) -> tuple[str, bool]:
    text = team.run_send_message(str(params.get("to", "")), str(params.get("content", "")))
    return (text, text.startswith("Teammate '") and "is not active" in text)


def _shutdown(params: dict[str, Any]) -> tuple[str, bool]:
    text = team.run_request_shutdown(str(params.get("teammate", "")))
    return (text, text.startswith("Teammate '") and "is not active" in text)


def _request_plan(params: dict[str, Any]) -> tuple[str, bool]:
    text = team.run_request_plan(str(params.get("teammate", "")), str(params.get("task", "")))
    return (text, text.startswith("Teammate '") and "is not active" in text)


def _review_plan(params: dict[str, Any]) -> tuple[str, bool]:
    text = team.run_review_plan(
        str(params.get("request_id", "")),
        bool(params.get("approve", False)),
        str(params.get("feedback", "")),
    )
    return (text, text.startswith("Request ") and ("not found" in text or "not a plan" in text or "already" in text or "belongs" in text or "is not the current" in text))


SPAWN_TEAMMATE_TOOL = Tool(
    schema={
        "name": "spawn_teammate",
        "description": (
            "Spawn a persistent teammate thread that can claim Tasks from the shared "
            "task system and report back results. Provide a role and an initial prompt; "
            "optionally bind the first Task and require plan approval before it edits files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "1-64 letters/digits/_/-"},
                "role": {"type": "string", "description": "e.g. 'reviewer'"},
                "prompt": {"type": "string", "description": "initial instructions"},
                "task_id": {"type": "string", "description": "optional first Task to claim"},
                "require_plan": {"type": "boolean", "description": "true = must submit plan for approval"},
            },
            "required": ["name", "role", "prompt"],
        },
    },
    handler=_spawn,
)


LIST_TEAMMATES_TOOL = Tool(
    schema={
        "name": "list_teammates",
        "description": "List active teammates and their current status.",
        "input_schema": {"type": "object", "properties": {}},
    },
    handler=_list_teammates,
)


SEND_MESSAGE_TOOL = Tool(
    schema={
        "name": "send_message",
        "description": "Send an intermediate message to an active teammate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["to", "content"],
        },
    },
    handler=_send,
)


REQUEST_SHUTDOWN_TOOL = Tool(
    schema={
        "name": "request_shutdown",
        "description": "Ask a teammate to finish its current step and shut down.",
        "input_schema": {
            "type": "object",
            "properties": {"teammate": {"type": "string"}},
            "required": ["teammate"],
        },
    },
    handler=_shutdown,
)


REQUEST_PLAN_TOOL = Tool(
    schema={
        "name": "request_plan",
        "description": "Require a teammate to submit a plan for approval before editing files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "teammate": {"type": "string"},
                "task": {"type": "string"},
            },
            "required": ["teammate", "task"],
        },
    },
    handler=_request_plan,
)


REVIEW_PLAN_TOOL = Tool(
    schema={
        "name": "review_plan",
        "description": "Approve or reject a teammate's submitted plan by request_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "approve": {"type": "boolean"},
                "feedback": {"type": "string"},
            },
            "required": ["request_id", "approve"],
        },
    },
    handler=_review_plan,
)


CREATE_WORKTREE_TOOL = Tool(
    schema={
        "name": "create_worktree",
        "description": (
            "Bind a pending Task to an isolated git worktree under .worktrees/<name>. "
            "Works only when the current directory is a Git repository."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["name", "task_id"],
        },
    },
    handler=lambda p: (team.run_create_worktree(str(p.get("name", "")), str(p.get("task_id", ""))),
                       False),
)