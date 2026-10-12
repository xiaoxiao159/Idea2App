"""mcp (s14) 冒烟:拼名归一 / 进程内 mock server 连接 / 工具发现 / host policy / 权限闸门。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())

from agent import mcp  # noqa: E402
from agent import tools  # noqa: E402
from agent.hooks import permission  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


# ---- 1. normalize_mcp_name ----
assert mcp.normalize_mcp_name("abc123") == "abc123"
assert mcp.normalize_mcp_name("my server!") == "my_server_"
try:
    mcp.normalize_mcp_name("")
    raise SystemExit("should have raised: empty name")
except ValueError:
    pass
ok("normalize_mcp_name: 非法字符换 '_',空名抛错")

# ---- 2. connect:首连 / 重复连 / 未知 server ----
mcp.MCP_CLIENTS.clear()
text = mcp.connect("docs")
assert text.startswith("Connected to MCP server 'docs'") and "search" in text
assert mcp.connect("docs") == "MCP server 'docs' already connected"
assert mcp.connect("nope").startswith("Unknown server 'nope'")
ok("connect: 首连回报发现工具,重复连/未知名如实回报")

# ---- 3. assembled_tools:前缀名 + handler + policy ----
mcp.connect("deploy")
tool_defs, handlers, policies = mcp.assembled_tools()
names = sorted(d["name"] for d in tool_defs)
assert names == [
    "mcp__deploy__status", "mcp__deploy__trigger",
    "mcp__docs__get_version", "mcp__docs__search",
], names
assert policies["mcp__docs__search"] == "allow"
assert policies["mcp__deploy__trigger"] == "confirm"
assert mcp.policy_for("mcp__docs__search") == "allow"
assert mcp.policy_for("mcp__nope__x") == "confirm"
ok("assembled_tools: mcp__server__tool 前缀名 + policy 表")

assert handlers["mcp__docs__search"](query="abc") == "[docs] Found 3 results for 'abc'"
assert handlers["mcp__deploy__status"](service="api") == "[deploy] api: running (v1.4.2)"
ok("handler: 走 MCPClient.call_tool,参数原样透传")

# ---- 4. 工具包装:connect_mcp 把发现的工具注册进 REGISTRY ----
text, err = tools.run_tool("connect_mcp", {"name": "docs"})
assert err is False and "already connected" in text
assert "mcp__docs__search" in tools.REGISTRY
text, err = tools.run_tool("mcp__docs__search", {"query": "xyz"})
assert err is False and "Found 3 results for 'xyz'" in text
ok("run_tool: connect_mcp 后 mcp__docs__search 可调")

# ---- 5. permission 对 mcp__* 走 host policy ----
assert permission.check("mcp__docs__search", {"query": "x"}) is None  # allow 直放
assert permission.check("mcp__deploy__trigger", {"service": "x"}) is not None  # confirm 未授权 → 否决串

# 会话内授权后放行
permission._GRANTS.add("mcp__deploy__trigger")
assert permission.check("mcp__deploy__trigger", {"service": "x"}) is None
permission._GRANTS.discard("mcp__deploy__trigger")
ok("permission: mcp__* allow 直放 / confirm 需授权")

# deny 不可谈判
mcp.mcp_tool_policies["mcp__deploy__trigger"] = "deny"
veto = permission.check("mcp__deploy__trigger", {"service": "x"})
assert veto is not None and "deny" in veto and "not negotiable" in veto
ok("permission: deny policy 不可谈判")

print(f"\nALL {PASS} assertions passed")