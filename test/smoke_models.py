"""models 冒烟:config 注册表校验 / 工厂分发 / openai 翻译层(纯函数,不打真 API)。"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import Config, ConfigError, build_config  # noqa: E402
from agent.models import get_model  # noqa: E402
from agent.models.base import Model  # noqa: E402
from agent.models.openai import (  # noqa: E402
    _messages_to_openai,
    _response_to_unified,
    _tools_to_openai,
)

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


# ---- 1. build_config:注册表驱动校验 ----
c = build_config({"MODEL_PROVIDER": "anthropic", "MODEL_ID": "claude-x",
                 "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "https://gw"})
assert c == Config(provider="anthropic", model="claude-x", api_key="k",
                   base_url="https://gw")
ok("build: anthropic 全量")

c = build_config({"MODEL_ID": "m", "ANTHROPIC_API_KEY": "k",
                 "ANTHROPIC_BASE_URL": "u"})
assert c.provider == "anthropic"
ok("build: 不写 MODEL_PROVIDER 默认 anthropic(老 .env 照跑,零迁移)")

c = build_config({"MODEL_PROVIDER": "OPENAI", "MODEL_ID": "deepseek-chat",
                  "OPENAI_API_KEY": "k"})
assert c.provider == "openai" and c.base_url == ""
ok("build: provider 大小写归一;openai 的 base_url 可空 → SDK 默认端点")

for bad_env, needle in [
    ({"MODEL_PROVIDER": "gemini"}, "MODEL_PROVIDER"),
    ({"MODEL_PROVIDER": "openai", "MODEL_ID": "m"}, "OPENAI_API_KEY"),
    ({"MODEL_PROVIDER": "openai", "MODEL_ID": "m", "OPENAI_API_KEY": "   "}, "OPENAI_API_KEY"),
    ({"MODEL_ID": "m", "ANTHROPIC_API_KEY": "k"}, "ANTHROPIC_BASE_URL"),
]:
    try:
        build_config(bad_env)
        raise SystemExit(f"should have raised: {bad_env}")
    except ConfigError as e:
        assert needle in str(e)
ok("build: 未知 provider / openai 缺 key(含空白串)/ anthropic 缺 base_url → ConfigError")

# ---- 2. get_model:工厂分发 ----
try:
    import anthropic  # noqa: F401
except ImportError:
    print("  skip - anthropic SDK 未安装,get_model('anthropic') 分支跳过")
else:
    m = get_model(build_config({"MODEL_PROVIDER": "anthropic", "MODEL_ID": "x",
                                "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "https://x"}))
    assert isinstance(m, Model) and type(m).__name__ == "AnthropicModel"
    ok("factory: anthropic → AnthropicModel(构造 client 不发网络请求)")

try:
    get_model(Config(provider="nope", model="m", api_key="k", base_url=""))
    raise SystemExit("should have raised")
except ConfigError:
    pass
ok("factory: 未知 provider → ConfigError(绕过 build_config 直接构造 Config 也拦得住)")

try:
    import openai  # noqa: F401
except ImportError:
    print("  skip - openai SDK 未安装,get_model('openai') 分支跳过")
else:
    m = get_model(build_config({"MODEL_PROVIDER": "openai", "MODEL_ID": "gpt-4o",
                                "OPENAI_API_KEY": "k"}))
    assert isinstance(m, Model) and type(m).__name__ == "OpenAIModel"
    ok("factory: openai → OpenAIModel")

# ---- 3. _tools_to_openai:壳不同,Schema 同体 ----
t = _tools_to_openai([{"name": "bash", "description": "run shell",
                       "input_schema": {"type": "object"}}])
assert t == [{"type": "function",
              "function": {"name": "bash", "description": "run shell",
                           "parameters": {"type": "object"}}}]
ok("tools: input_schema 挪个位置叫 parameters")

# ---- 4. _messages_to_openai:块级翻译 ----
messages = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": [
        {"type": "text", "text": "thinking aloud"},
        {"type": "thinking", "thinking": "...", "signature": "sig"},
        {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "file.txt"},
        {"type": "text", "text": "and this too"},
    ]},
]
out = _messages_to_openai(messages, "be good")
assert out[0] == {"role": "system", "content": "be good"}
assert out[1] == {"role": "user", "content": "hi"}
assert out[2] == {"role": "assistant", "content": "thinking aloud",
                  "tool_calls": [{"id": "t1", "type": "function",
                                  "function": {"name": "bash",
                                               "arguments": '{"command": "ls"}'}}]}
assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "file.txt"}
assert out[4] == {"role": "user", "content": "and this too"}
assert len(out) == 5
ok("messages: system 直通 / text 保留 / thinking 丢弃 / tool_use→tool_calls / tool_result→role=tool")

out = _messages_to_openai(
    [{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x",
                                       "input": {}}]}], "s")
assert "content" not in out[1] and out[1]["tool_calls"][0]["function"]["arguments"] == "{}"
ok("messages: 纯调用轮不带 content 键;空 input 序列化为 '{}'")

# ---- 5. _response_to_unified:出向翻译 ----


def fake_response(finish, text=None, calls=None):
    msg = SimpleNamespace(content=text, tool_calls=calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
    )


def fake_call(cid, name, args_json):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args_json))


r = _response_to_unified(fake_response("stop", text="done"))
assert r == {"stop_reason": "end_turn",
             "content": [{"type": "text", "text": "done"}],
             "usage": {"input": 100, "output": 50}}
ok("response: stop→end_turn;usage prompt/completion → input/output")

r = _response_to_unified(fake_response(
    "tool_calls", calls=[fake_call("c1", "bash", '{"command": "ls"}')]))
assert r["stop_reason"] == "tool_use"
assert r["content"] == [{"type": "tool_use", "id": "c1", "name": "bash",
                         "input": {"command": "ls"}}]
ok("response: tool_calls→tool_use;JSON 字符串参数翻回 dict")

r = _response_to_unified(fake_response(
    "tool_calls", calls=[fake_call("c1", "bash", '{"command": "ls')]))
assert r["content"][0]["input"] == {}
ok("response: 截断的坏 JSON → input={} 回填,协议不脱节")

assert _response_to_unified(fake_response("length"))["stop_reason"] == "max_tokens"
ok("response: length→max_tokens")

assert _response_to_unified(fake_response("content_filter"))["stop_reason"] == "content_filter"
ok("response: 未知 finish_reason 原样透传(loop 的异常终态分支兜住)")

assert _response_to_unified(fake_response("stop"))["content"] == []
ok("response: 空产出 → content=[],loop 注入 (empty response)")

print(f"\nALL {PASS} assertions passed")
