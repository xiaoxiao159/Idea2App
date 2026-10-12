from __future__ import annotations

from typing import Any

from agent.tools.base import Tool
from agent.tools.bash import BASH_TOOL
from agent.tools.compact import COMPACT_TOOL
from agent.tools.cron import CANCEL_CRON_TOOL, LIST_CRONS_TOOL, SCHEDULE_CRON_TOOL
from agent.tools.files import EDIT_TOOL, READ_TOOL, WRITE_TOOL
from agent.tools.mcp import CONNECT_MCP_TOOL
from agent.tools.skill import SKILL_TOOL
from agent.tools.task import TASK_TOOL
from agent.tools.tasks import (
    CLAIM_TASK_TOOL,
    COMPLETE_TASK_TOOL,
    CREATE_TASK_TOOL,
    GET_TASK_TOOL,
    LIST_TASKS_TOOL,
    UPDATE_TASK_TOOL,
)
from agent.tools.team import (
    CREATE_WORKTREE_TOOL,
    LIST_TEAMMATES_TOOL,
    REQUEST_PLAN_TOOL,
    REQUEST_SHUTDOWN_TOOL,
    REVIEW_PLAN_TOOL,
    SEND_MESSAGE_TOOL,
    SPAWN_TEAMMATE_TOOL,
)
from agent.tools.todo import TODO_TOOL
from agent.tools.workflow import WORKFLOW_TOOL

REGISTRY: dict[str, Tool] = {
    t.schema["name"]: t
    for t in (
        BASH_TOOL, READ_TOOL, WRITE_TOOL, EDIT_TOOL, TODO_TOOL, SKILL_TOOL,
        COMPACT_TOOL, TASK_TOOL,
        CREATE_TASK_TOOL, UPDATE_TASK_TOOL, LIST_TASKS_TOOL,
        GET_TASK_TOOL, CLAIM_TASK_TOOL, COMPLETE_TASK_TOOL,
        # s12 定时任务
        SCHEDULE_CRON_TOOL, LIST_CRONS_TOOL, CANCEL_CRON_TOOL,
        # s14 MCP(连上后把 mcp__server__tool 动态注册进来)
        CONNECT_MCP_TOOL,
        # s13 团队:Lead 侧
        SPAWN_TEAMMATE_TOOL, LIST_TEAMMATES_TOOL, SEND_MESSAGE_TOOL,
        REQUEST_SHUTDOWN_TOOL, REQUEST_PLAN_TOOL, REVIEW_PLAN_TOOL,
        CREATE_WORKTREE_TOOL,
        # s16 编排运行时
        WORKFLOW_TOOL,
    )
}


def api_tools() -> list[dict]:
    return [tool.schema for tool in REGISTRY.values()]


def run_tool(name: str, params: dict[str, Any]) -> tuple[str, bool]:
    tool = REGISTRY.get(name)
    if tool is None:
        return (f"Error: unknown tool '{name}'", True)
    try:
        return tool.handler(params)
    except Exception as exc:
        return (f"Error: {type(exc).__name__}: {exc}", True)


def summary(name: str, params: dict[str, Any]) -> str:
    tool = REGISTRY.get(name)
    if tool is None:
        return f"{name} (unknown tool)"
    return tool.shown(params)
