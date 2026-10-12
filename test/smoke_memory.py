"""memory 冒烟:AGENTS.md 加载 / 会话转录往返 / 事实 markdown / replay / 摘要折叠。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import context  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.context import archive, compact, meter  # noqa: E402
from agent.memory import agentsmd, facts, sessions  # noqa: E402
from agent.models.base import Model  # noqa: E402

# 所有落盘都进临时目录,不污染项目
TMP = Path(tempfile.mkdtemp())
os.chdir(TMP)
USER, PROJ = TMP / "home", TMP / "proj"
USER.mkdir(exist_ok=True)
PROJ.mkdir(exist_ok=True)

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


def cf(window=100_000, max_tokens=100):
    return Config(provider="anthropic", model="m", api_key="k", base_url="u",
                  window=window, max_tokens=max_tokens)


# ---- 1. agentsmd:两级拼接,user 在前 project 在后 ----
(USER / "AGENTS.md").write_text("用户级:偏好简洁", encoding="utf-8")
(PROJ / "AGENTS.md").write_text("项目级:用 FastAPI", encoding="utf-8")
out = agentsmd.load(USER, PROJ)
assert "用户级" in out and "项目级" in out and out.index("用户级") < out.index("项目级")
ok("agentsmd: user 在前 project 在后,都进 system")

assert agentsmd.load(TMP / "none1", TMP / "none2") == ""
ok("agentsmd: 目录不存在 → 空串,调用方无需 if")

# ---- 2. sessions:record/read 往返 + 标题 + finish + list + attach ----
root = PROJ / "sessions"
s = sessions.begin(root)
sessions.record({"role": "user", "content": "帮我修 auth"})
sessions.record({"role": "assistant", "content": [{"type": "text", "text": "好"}]})
msgs = sessions.read_messages(s)
assert len(msgs) == 2 and msgs[0]["role"] == "user" and msgs[1]["content"][0]["text"] == "好"
ok("sessions: record 两条 → read 原样读回(形状 = emit 投影)")

assert sessions.read_meta(s).get("title") == "帮我修 auth"
ok("sessions: 第一条 user 原话当标题")

sessions.finish(usage={"input": 100, "output": 50})
assert sessions.read_meta(s).get("closed") is True
assert sessions.read_meta(s)["input"] == 100 and sessions.read_meta(s)["output"] == 50
ok("sessions: finish 钉 closed + 记用量")

lst = sessions.list_sessions(root)
assert [x.id for x in lst] == [s.id]
ok("sessions: list_sessions 扫得回;目录名(时间戳)即单机索引")

sessions.attach(s)
sessions.record({"role": "user", "content": "继续"})
assert len(sessions.read_messages(s)) == 3
assert sessions.read_meta(s).get("closed") is False
ok("sessions: attach 重开旧会话,继续追加同一文件")

sessions.CURRENT = None
sessions.record({"role": "user", "content": "x"})
ok("sessions: 无活跃会话时 record 是 no-op(测试直接调 loop 不受影响)")

# ---- 3. facts:注入 + 追加 ----
um, pm = USER / "memory", PROJ / "memory"
um.mkdir(parents=True, exist_ok=True)
pm.mkdir(parents=True, exist_ok=True)
(um / "profile.md").write_text("偏好: 用中文", encoding="utf-8")
(pm / "project.md").write_text("决定: 用 SQLite", encoding="utf-8")
(pm / "entities.md").write_text("auth.py: 中间件", encoding="utf-8")
inj = facts.inject(um, pm)
assert "偏好: 用中文" in inj and "决定: 用 SQLite" in inj and "auth.py" in inj
ok("facts: inject 把 profile/project/entities 三段都拼进 system")

facts.append_notes(pm, "新结论: 换 Pydantic v2")
assert "新结论: 换 Pydantic v2" in (pm / "project.md").read_text(encoding="utf-8")
ok("facts: append_notes 只加不覆盖人手写内容")

# ---- 4. replay:pin 规则照搬入账语义 ----
context.reset()
context.setup("sys", [{"name": "bash", "description": "d",
                       "input_schema": {"type": "object"}}], cf())
context.replay([
    {"role": "user", "content": "历史用户原话"},
    {"role": "assistant", "content": [{"type": "text", "text": "历史回复"}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "out"}]},
])
assert context.WINDOW[0].pin is True and context.WINDOW[1].pin is False and context.WINDOW[2].pin is False
ok("replay: 历史原话 pin=True,assistant/工具结果 pin=False")

# ---- 5. 摘要折叠:_summarize_pass 成功 / 价值不够 / 没模型 ----
class Echor(Model):
    def complete(self, messages, system, tools, max_tokens=8000):
        return {"content": [{"type": "text", "text": "摘要: 用 FastAPI 重构 auth"}],
                "stop_reason": "end_turn", "usage": {"input": 10, "output": 5}}


archive.clear()
context.reset()
context.setup("sys", [], cf())
archive.push("原始原文" * 100, 2500, "raw_turn")   # tokens >= MIN_SAVE(2000)
context.append_assistant([{"type": "text", "text": "tail 原话"}])
context.WINDOW.insert(0, meter.stamp(
    {"role": "assistant", "content": "[归档] 一段助手回复(原 2,500 tok 已入档案 ...)"},
    pin=False, epoch=0))

assert compact._summarize_pass(Echor()) is True
msgs = context.emit()
assert any(m.get("content") == "摘要: 用 FastAPI 重构 auth" for m in msgs)
assert not any(isinstance(m.get("content"), str) and "[归档]" in m.get("content") for m in msgs)
assert any(m["content"] == [{"type": "text", "text": "tail 原话"}] for m in msgs)
assert archive.size() == 0  # 原文已被摘要扛下,索引+盘一起清
ok("compact: _summarize_pass 把占位折成摘要、保留 tail 原话、清掉原文")

archive.clear()
archive.push("短" * 10, 100, "raw_turn")  # < MIN_SAVE
assert compact._summarize_pass(Echor()) is False
ok("compact: 价值不满 MIN_SAVE → 不烧 LLM,降级保命")

archive.clear()
assert compact._summarize_pass(None) is False
ok("compact: 没模型 → 降级")

print(f"\nALL {PASS} assertions passed")