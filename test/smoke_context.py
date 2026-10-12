"""context 冒烟:估算 / 计数 / 盖章 / 预算公式 / 液位 / 入账-投影回路 / config 供给。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import context  # noqa: E402
from agent.config import Config, ConfigError, build_config  # noqa: E402
from agent.context import meter  # noqa: E402
from agent.context.base import Budget, Entry  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


# ---- 1. estimate_tokens:chars/N 往高估,空串也占 1 ----
assert meter.estimate_tokens("") == 1; ok("estimate: 空串也占 1")
assert meter.estimate_tokens("abc") == 1; ok("estimate: 3 chars/3 = 1")
assert meter.estimate_tokens("abcd") == 2; ok("estimate: 4 chars 向上取整 → 2")

# ---- 2. count_message:str / 块列表 / 垃圾形状都不炸 ----
assert meter.count_message({"content": "abcdef"}) == 2; ok("count: str 内容数字符")
assert meter.count_message({"role": "user"}) == 1; ok("count: 缺 content 也兜成 1,不炸")
assert meter.count_message({"content": []}) == 1; ok("count: 空块列表给 1")
assert meter.count_message({"content": [{"type": "text", "text": "abcdef"}]}) == 2
ok("count: text 块按文本数")
assert meter.count_message({"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "abcdef"}]}) == 2
ok("count: tool_result 按结果文本数")
assert isinstance(meter.count_message({"content": [{"type": "thinking", "thinking": "..."}]}), int)
ok("count: 未知块类型走 json 兜底,不炸")

# ---- 3. stamp:盖章出 Entry,账齐全 ----
e = meter.stamp({"role": "user", "content": "abcdef"}, pin=True, epoch=0)
assert isinstance(e, Entry) and e.pin is True and e.epoch == 0 and e.tokens == 2
ok("stamp: 账(tokens/pin/epoch)齐")

# ---- 4. 预算公式:floor(window×(1−reserve)) − max_tokens ----
b = meter.make_budget(40_000, 2_000, 0.2)
assert isinstance(b, Budget) and b.input_budget == 30_000
ok("budget: 40000*0.8 - 2000 = 30000")

# ---- 5. 液位:(基线 + Σtokens) / 预算;空/溢出边界 ----
entries = [
    meter.stamp({"role": "user", "content": "a" * 30}, True, 0),        # 10
    meter.stamp({"role": "assistant", "content": "b" * 30}, False, 0),  # 10
]
assert abs(meter.pressure(entries, 30, b) - 50 / 30_000) < 1e-9
ok("pressure: (30 + 20) / 30000")
assert meter.pressure([], 0, b) == 0.0; ok("pressure: 空窗口 + 空基线 = 0")
assert meter.pressure(entries, 0, meter.make_budget(100, 200, 0.0)) == 1.0
ok("pressure: 预算为负 → 报满 1.0,不除零")

# ---- 6. 入账 → 投影回路:顺序与 pin 记账 ----
context.reset()
cfg = Config(provider="anthropic", model="m", api_key="k", base_url="u",
             window=40_000, max_tokens=2_000)
context.setup("sys prompt", [{"name": "bash", "description": "d",
                              "input_schema": {"type": "object"}}], cfg)
assert context.pressure_line().startswith("pressure ")
ok("setup: 报得出 pressure_line")
context.append_user("hello")
context.append_assistant([{"type": "text", "text": "hi"}])
context.append_results([{"type": "tool_result", "tool_use_id": "t1", "content": "x"}])
msgs = context.emit()
assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
assert msgs[0]["content"] == "hello" and msgs[2]["content"][0]["type"] == "tool_result"
ok("回路: append_user/assistant/results → emit 投影回纯 messages,顺序一致")
assert context.WINDOW[0].pin is True and context.WINDOW[2].pin is False
ok("回路: pin 靠入账显式判,不靠 role 猜(tool_result 也是 role=user)")

# ---- 7. reset 隔离 ----
context.reset()
assert context.emit() == [] and context.pressure_line() == "context: not set up"
ok("reset: 清空窗口 + 抹基线/预算")

# ---- 8. config 供给:覆盖 / 缺省 / 显式设错 ----
c = build_config({"MODEL_PROVIDER": "anthropic", "MODEL_ID": "x",
                  "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "https://x",
                  "CONTEXT_WINDOW": "12345", "MAX_TOKENS": "500"})
assert c.window == 12_345 and c.max_tokens == 500
ok("config: CONTEXT_WINDOW / MAX_TOKENS 覆盖默认")
c = build_config({"MODEL_PROVIDER": "anthropic", "MODEL_ID": "x",
                  "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "https://x"})
assert c.window == 200_000 and c.max_tokens == 8_000
ok("config: 缺省 → provider 默认表 anthropic=200k, max_tokens=8000")
for bad in ("abc", "0", "-5"):
    try:
        build_config({"MODEL_PROVIDER": "anthropic", "MODEL_ID": "x",
                      "ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "https://x",
                      "CONTEXT_WINDOW": bad})
        raise SystemExit(f"should have raised: {bad!r}")
    except ConfigError as exc:
        assert "CONTEXT_WINDOW" in str(exc)
ok("config: 显式设错(非整数/非正)→ ConfigError")

print(f"\nALL {PASS} assertions passed")