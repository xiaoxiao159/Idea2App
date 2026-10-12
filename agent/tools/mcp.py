"""mcp 工具:connect_mcp(schema + handler + display)+ 把发现的 MCP 工具注册进 REGISTRY。

引擎在 agent/mcp.py;这里负责「连上之后把 mcp__server__tool 变成真工具」:
静态 REGISTRY 是 api_tools() 的唯一真相源,connect 成功后把该 server 的工具
以 Tool 对象挂进去,handler 走 MCPClient.call_tool。policy 已经由 mcp.assembled_tools()
写进 mcp.mcp_tool_policies,permission 闸门读那份表。
"""
from __future__ import annotations

from typing import Any

from agent import mcp
from agent import tools
from agent.tools.base import Tool


def _register_connected() -> None:
    """把当前所有已连 server 的工具注册进全局 REGISTRY(幂等:同名覆盖为新 handler)。"""
    tool_defs, handlers, _policies = mcp.assembled_tools()
    for tool_def in tool_defs:
        name = tool_def["name"]
        handler = handlers[name]

        def _call(params: dict[str, Any], _handler=handler) -> tuple[str, bool]:
            text = _handler(**params)
            is_error = text.startswith("MCP error:")
            return (text, is_error)

        tools.REGISTRY[name] = Tool(
            schema={
                "name": name,
                "description": tool_def.get("description", ""),
                "input_schema": tool_def.get("input_schema", {"type": "object", "properties": {}}),
            },
            handler=_call,
        )


def _connect(params: dict[str, Any]) -> tuple[str, bool]:
    name = str(params.get("name", "")).strip()
    text = mcp.connect(name)
    is_error = text.startswith("Unknown server") or text.startswith("MCP server") and "already" not in text
    if name in mcp.MCP_CLIENTS:
        _register_connected()
    return (text, is_error)


def _display(params: dict[str, Any]) -> str:
    return f"mcp+ {params.get('name', '?')}"


CONNECT_MCP_TOOL = Tool(
    schema={
        "name": "connect_mcp",
        "description": (
            "Connect to an MCP server and discover its tools (they appear as "
            "mcp__<server>__<tool>). Call this before using any mcp__ tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": ["docs", "deploy"]}},
            "required": ["name"],
        },
    },
    handler=_connect,
    display=_display,
)