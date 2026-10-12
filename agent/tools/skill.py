"""skill 工具:模型侧触发通路。命中 → 包裹正文作为 tool_result(L2 披露)。"""
from __future__ import annotations

from typing import Any

from agent import skills
from agent.tools.base import Tool


def _invoke(params: dict[str, Any]) -> tuple[str, bool]:
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return ("Error: missing required parameter 'name'", True)
    skill = skills.get_skill(name)
    if skill is None:
        available = ", ".join(sorted(skills._SKILLS)) or "(none)"
        return (f"Error: unknown skill '{name}'. Available: {available}", True)
    return (skills.render_invocation(skill, str(params.get("args", ""))), False)


def _disp(params: dict[str, Any]) -> str:
    # display 在校验之前跑(loop 打印、permission/audit 都会调),什么形状都不许炸
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return "skill (no name)"
    return f"skill({name})"


SKILL_TOOL = Tool(
    schema={
        "name": "skill",
        "description": (
            "Invoke a packaged skill (reusable workflow instructions). "
            "Before starting a task, check the 'Available skills' section "
            "in the system prompt: if a skill's description matches the task, "
            "call this tool with that skill's name to load the full instructions, "
            "then follow them. Supporting files referenced by the skill "
            "can be read with the read tool from the skill's directory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "exact skill name from the Available skills list",
                },
                "args": {
                    "type": "string",
                    "description": "optional task arguments to pass through",
                },
            },
            "required": ["name"],
        },
    },
    handler=_invoke,
    display=_disp,
)
