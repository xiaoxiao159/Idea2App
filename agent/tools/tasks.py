"""task 工具:6 个任务图工具(schema + handler + display)。

薄壳:逻辑与状态在 tasks/store.py;这里只做 schema 翻译、把 store 的返回捏成
(text, is_error)。任务图回答"大目标拆成的小任务谁做、按什么顺序",todo_write
是"当前任务自己的执行清单"——两者并存,各管一档事。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from agent.tasks import store
from agent.tools.base import Tool

_MARKS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def _render_list() -> str:
    tasks = store.TASKS.list()
    if not tasks:
        return "no tasks. use create_task to add some."
    lines = []
    for t in tasks:
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(
            f"{_MARKS.get(t.status, '[?]')} {t.id}: {t.subject} "
            f"[{t.status}]{owner}{deps}"
        )
    return "\n".join(lines)


def _create(params: dict[str, Any]) -> tuple[str, bool]:
    subject = str(params.get("subject", "")).strip()
    description = str(params.get("description", "")).strip()
    task = store.TASKS.create(subject, description)
    return (f"created {task.id}: {task.subject}", False)


def _update(params: dict[str, Any]) -> tuple[str, bool]:
    task_id = str(params["task_id"])
    add = list(params["addBlockedBy"]) if isinstance(params.get("addBlockedBy"), list) else []
    task = store.TASKS.update_dependencies(task_id, add)
    deps = ", ".join(task.blockedBy) or "(none)"
    return (f"updated {task.id} blockedBy: {deps}", False)


def _list(params: dict[str, Any]) -> tuple[str, bool]:
    return (_render_list(), False)


def _get(params: dict[str, Any]) -> tuple[str, bool]:
    task = store.TASKS.load(str(params["task_id"]))
    return (json.dumps(asdict(task), indent=2, ensure_ascii=False), False)


def _claim(params: dict[str, Any]) -> tuple[str, bool]:
    return store.claim(str(params["task_id"]), owner="agent")


def _complete(params: dict[str, Any]) -> tuple[str, bool]:
    return store.complete(str(params["task_id"]), owner="agent")


def _disp_create(params: dict[str, Any]) -> str:
    return f"task+ {params.get('subject', '')}"


def _disp_update(params: dict[str, Any]) -> str:
    return f"task~ {params.get('task_id', '?')}"


def _disp_list(params: dict[str, Any]) -> str:
    return "task list"


def _disp_get(params: dict[str, Any]) -> str:
    return f"task? {params.get('task_id', '?')}"


def _disp_claim(params: dict[str, Any]) -> str:
    return f"claim {params.get('task_id', '?')}"


def _disp_complete(params: dict[str, Any]) -> str:
    return f"done {params.get('task_id', '?')}"


CREATE_TASK_TOOL = Tool(
    schema={
        "name": "create_task",
        "description": "Create a task node and return its runtime-generated id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "short task label"},
                "description": {"type": "string", "description": "optional details"},
            },
            "required": ["subject"],
        },
    },
    handler=_create,
    display=_disp_create,
)

UPDATE_TASK_TOOL = Tool(
    schema={
        "name": "update_task",
        "description": (
            "Add dependencies to a pending, unclaimed task using ids returned by create_task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"},
                "addBlockedBy": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^task_[0-9a-f]{8}$"},
                    "minItems": 1,
                },
            },
            "required": ["task_id", "addBlockedBy"],
        },
    },
    handler=_update,
    display=_disp_update,
)

LIST_TASKS_TOOL = Tool(
    schema={
        "name": "list_tasks",
        "description": "List all tasks with status, owner, and dependencies.",
        "input_schema": {"type": "object", "properties": {}},
    },
    handler=_list,
    display=_disp_list,
)

GET_TASK_TOOL = Tool(
    schema={
        "name": "get_task",
        "description": "Get a task's full record by id.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    handler=_get,
    display=_disp_get,
)

CLAIM_TASK_TOOL = Tool(
    schema={
        "name": "claim_task",
        "description": "Claim a pending task whose dependencies are all completed.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    handler=_claim,
    display=_disp_claim,
)

COMPLETE_TASK_TOOL = Tool(
    schema={
        "name": "complete_task",
        "description": "Mark a task claimed by this agent as completed (unblocks dependents).",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    handler=_complete,
    display=_disp_complete,
)