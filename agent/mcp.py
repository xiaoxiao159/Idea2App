"""mcp:s14 —— 在进程内接外部工具,MCP 工具名的生成与授权全在此。

照参考实现 s14_mcp_plugin,刻意不做真实 stdio transport —— README 明说「真实 MCP
transport 不在本章实现」:这里用两个进程内 mock server(docs / deploy)演示
「发现(list)→ 拼名(mcp__server__tool)→ 授权(host policy)→ 调用(call)」这条链。

关键决策照参考实现:
- 工具名 `mcp__{server}__{tool}` 由 host 拼,不信任 server 自己报的名字;
  `normalize_mcp_name` 把非 [a-zA-Z0-9_-] 字符归一成 `_`,避免模型工具名字符集问题。
- 授权来自 host 配置 MCP_HOST_POLICY,永不来自 server 的 description;
  allow / confirm / deny 三态,缺省 confirm(要问人)。
"""
from __future__ import annotations

import re
from typing import Callable

_DISALLOWED_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


class MCPClient:
    """进程内 MCP tools/list 与 tools/call 的替身。一个 client = 一个 server。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, Callable] = {}

    def register(self, tool_defs: list[dict], handlers: dict[str, Callable]) -> None:
        names = [tool.get("name") for tool in tool_defs]
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Every MCP tool needs a non-empty name")
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate MCP tool name on server {self.name!r}")
        missing = [name for name in names if name not in handlers]
        if missing:
            raise ValueError(f"Missing MCP handlers: {', '.join(missing)}")
        self.tools = list(tool_defs)
        self._handlers = dict(handlers)

    def call_tool(self, tool_name: str, args: dict) -> str:
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return str(handler(**args))
        except Exception as exc:
            return f"MCP error: {type(exc).__name__}: {exc}"


def normalize_mcp_name(name: str) -> str:
    """模型工具名字母表之外的字符统一换成 `_`;空结果说明名字没法用,抛错。"""
    normalized = _DISALLOWED_CHARS.sub("_", name)
    if not normalized:
        raise ValueError("MCP names cannot normalize to an empty string")
    return normalized


# 授权来自 host 配置,永不来自 server 的自述。缺省 confirm。
MCP_HOST_POLICY: dict[tuple[str, str], str] = {
    ("docs", "search"): "allow",
    ("docs", "get_version"): "allow",
    ("deploy", "status"): "allow",
    ("deploy", "trigger"): "confirm",
}


def _mock_server_docs() -> MCPClient:
    server = MCPClient("docs")
    server.register(
        tool_defs=[
            {
                "name": "search",
                "description": "Search the documentation.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "get_version",
                "description": "Get the documentation API version.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        },
    )
    return server


def _mock_server_deploy() -> MCPClient:
    server = MCPClient("deploy")
    server.register(
        tool_defs=[
            {
                "name": "trigger",
                "description": "Trigger a deployment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
                "annotations": {"destructiveHint": True},
            },
            {
                "name": "status",
                "description": "Check deployment status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
                "annotations": {"readOnlyHint": True},
            },
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        },
    )
    return server


MOCK_SERVERS: dict[str, Callable[[], MCPClient]] = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}

# 已连接的 server(name -> client)。进程内短命,进程死即亡。
MCP_CLIENTS: dict[str, MCPClient] = {}

# 拼出来的工具名 -> 授权策略;connect 时更新,permission 闸门读它。
mcp_tool_policies: dict[str, str] = {}


def connect(name: str) -> str:
    """连一个 mock server;已连 / 未知名字都如实回报。"""
    if name in MCP_CLIENTS:
        return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if not factory:
        return f"Unknown server '{name}'. Available: {', '.join(MOCK_SERVERS)}"
    server = factory()
    MCP_CLIENTS[name] = server
    names = ", ".join(tool["name"] for tool in server.tools)
    print(f"  [mcp] connected: {name} -> {names}")
    return (
        f"Connected to MCP server '{name}'. "
        f"Discovered {len(server.tools)} tools: {names}"
    )


def assembled_tools() -> list[dict]:
    """把当前所有已连 server 的工具拼成前缀名 `mcp__server__tool` 的工具定义列表。

    返回 (工具定义, handler, policy) 三元组;侧作用是把 policy 写进 mcp_tool_policies。
    """
    tools: list[dict] = []
    handlers: dict[str, Callable] = {}
    policies: dict[str, str] = {}
    origins: dict[str, str] = {}

    for server_name, server in MCP_CLIENTS.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in server.tools:
            raw_name = tool_def["name"]
            safe_tool = normalize_mcp_name(raw_name)
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            if len(prefixed) > 64:
                raise ValueError(f"MCP tool name is longer than 64 characters: {prefixed}")
            origin = f"MCP tool {server_name!r}/{raw_name!r}"
            if prefixed in origins:
                raise ValueError(
                    "MCP tool name collision after normalization: "
                    f"{prefixed!r} maps both {origins[prefixed]} and {origin}"
                )
            schema = tool_def.get("inputSchema", {})
            if not isinstance(schema, dict) or schema.get("type", "object") != "object":
                raise ValueError(f"Invalid input schema for {origin}")
            origins[prefixed] = origin
            tools.append({
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": schema,
            })
            handlers[prefixed] = (
                lambda *, client=server, tool=raw_name, **kwargs: client.call_tool(tool, kwargs)
            )
            policies[prefixed] = MCP_HOST_POLICY.get((server_name, raw_name), "confirm")

    mcp_tool_policies.clear()
    mcp_tool_policies.update(policies)
    return tools, handlers, policies


def policy_for(tool_name: str) -> str:
    """permission 闸门用:某个 mcp__ 工具名当前的授权策略。"""
    return mcp_tool_policies.get(tool_name, "confirm")


def assemble_system_prompt(base: str) -> str:
    """把「当前已连接的 MCP server」写进 system prompt(见参考 s14)。

    没连任何 server 时原样返回 base;连了则追加一行,提醒模型还有哪些外部工具可用。
    """
    if not MCP_CLIENTS:
        return base
    return base + "\n\nConnected MCP servers: " + ", ".join(sorted(MCP_CLIENTS))