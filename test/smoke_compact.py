"""compact 冒烟:回合切割 / 挑选护栏 / 落盘档案 / 阈值归档 / force / reactive / compact 工具。"""
import builtins
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# chdir 必须先于 import agent:team 在 import 时把 WORKDIR 钉在 launch cwd,
# 若晚于 import,工具基准(workspace.resolve_cwd)会留在仓库根,和临时档案目录分叉。
os.chdir(tempfile.mkdtemp())  # 档案落盘、audit 日志都落在临时目录,不污染项目

from agent import context, hooks, loop, tools  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.context import archive, compact, meter, select  # noqa: E402
from agent.hooks import permission, plan  # noqa: E402
from agent.models.base import Model  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


def cf(window=100_000, max_tokens=100):
    return Config(provider="anthropic", model="m", api_key="k", base_url="u",
                  window=window, max_tokens=max_tokens)


def setup_sys(cfg):
    context.setup("sys", [{"name": "bash", "description": "d",
                           "input_schema": {"type": "object"}}], cfg)


def u(text):
    return meter.stamp({"role": "user", "content": text}, pin=True, epoch=0)


def a_text(text):
    return meter.stamp({"role": "assistant", "content": [{"type": "text", "text": text}]},
                       pin=False, epoch=0)


def a_tool(tool_id):
    return meter.stamp(
        {"role": "assistant",
         "content": [{"type": "tool_use", "id": tool_id, "name": "bash",
                      "input": {"command": "ls"}}]},
        pin=False, epoch=0,
    )


def r_tool(tool_id, text, pin=False):
    return meter.stamp(
        {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]},
        pin=pin, epoch=0,
    )


# ---- 1. turns:assistant 起头,紧邻 tool_result 圈成一对 ----
entries = [u("go"), a_tool("t1"), r_tool("t1", "out")]
ts = select.turns(entries)
assert len(ts) == 1 and ts[0].start == 1 and ts[0].end == 2 and ts[0].kind == "tool_result"
ok("turns: assistant+tool_result 圈成一个 tool 回合")

entries = [u("go"), a_text("hi")]
ts = select.turns(entries)
assert len(ts) == 1 and ts[0].kind == "raw_turn" and (ts[0].start, ts[0].end) == (1, 1)
ok("turns: 纯文本 assistant 是单人回合")

# ---- 2. pick:尾部回合保留,tool 回合先选 ----
entries = [u("go"), a_tool("t1"), r_tool("t1", "out"), a_text("done")]
p = select.pick(entries)
assert len(p) == 1 and p[0].kind == "tool_result" and p[0].start == 1
ok("pick: 只选 tool 回合,最新尾回合 'done' 保留")

# ---- 3. pick:pin 成员 / 进行中 / 占位条目都被拦下 ----
entries = [u("go"), a_tool("t1"), r_tool("t1", "out", pin=True), a_text("done")]
assert select.pick(entries) == []
ok("pick: 回合里有 pin 成员 → 不选")

entries = [u("go"), a_tool("t9"), a_text("done")]
assert select.pick(entries) == []
ok("pick: 进行中的 tool_use(无结果)→ 不选")

syn = meter.stamp({"role": "assistant", "content": "[归档] 一段助手回复(...)"}, pin=False, epoch=0)
entries = [u("go"), syn, a_text("done")]
assert select.pick(entries) == []
ok("pick: 归档占位(str content)→ 不反复归档")

# ---- 4. archive:push(落盘) / read / fetch / flatten / placeholder ----
archive.clear()
blk = archive.push("raw text A", 5, "tool_result")
assert archive.size() == 1 and archive.tokens() == 5
assert blk.path.endswith("tool_result_0001.txt")
assert archive.read(blk) == "raw text A"
ok("archive: push 落盘,read 取回原文,索引只记路径")

text, err = tools.run_tool("read", {"file_path": blk.path})
assert err is False and "raw text A" in text
ok("read 工具: 占位里的相对路径 .idea2app/archive/… 能直接读回(模型侧召回成立)")

assert archive.fetch(keyword="text A") == [blk]
assert archive.fetch(keyword="nope") == [] and archive.fetch(kind="raw_turn") == []
ok("archive: fetch 按子串/kind 过滤(读文件比对),最新优先")

fl = archive.flatten({"role": "user", "content": [
    {"type": "text", "text": "hi"},
    {"type": "tool_use", "id": "t", "name": "bash", "input": {"command": "ls"}},
]})
assert "hi" in fl and "[tool] bash" in fl
ok("archive: flatten 把消息拍成可读文本")

ph = archive.placeholder("tool_result", 4200, ".idea2app/archive/tool_result_0001.txt")
assert ph["role"] == "assistant" and isinstance(ph["content"], str)
assert "[归档]" in ph["content"] and "4,200" in ph["content"] and "tool_result_0001.txt" in ph["content"]
ok("archive: placeholder 是 assistant + str,带归档标签 / tok 数 / 落盘路径")

# ---- 5. compact 端到端:超标 → 归档 tool 回合 → 液位回落 ----
archive.clear()
context.reset()
setup_sys(cf(window=1000, max_tokens=100))   # input_budget = 800 - 100 = 700
context.append_user("go")
context.append_assistant([{"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}}])
context.append_results([{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 2000}])
context.append_assistant([{"type": "text", "text": "done"}])

assert meter.pressure(context.WINDOW, context._BASELINE, context._BUDGET) >= compact.SAFE
ok("compact: 灌满后液位越过 SAFE")

rows_before = len(context.WINDOW)
compact.compact(None)
assert len(context.WINDOW) == rows_before - 1   # tool 回合(2 条)→ 1 条占位
ok("compact: 一个 tool 回合被 1 条占位替掉")

assert archive.size() == 1 and archive.fetch(keyword="xxx")[0].kind == "tool_result"
ok("compact: 原件落了盘,kind=tool_result")

assert context._EPOCH == 1
ok("compact: 归档后轮次 +1")

assert meter.pressure(context.WINDOW, context._BASELINE, context._BUDGET) < compact.TARGET
ok("compact: 归档后液位回落到 TARGET 以下")

roles = [m["role"] for m in context.emit()]
assert roles == ["user", "assistant", "assistant"]
assert any(isinstance(m["content"], str) and "[归档]" in m["content"] for m in context.emit())
assert any(m["content"] == [{"type": "text", "text": "done"}] for m in context.emit())
ok("compact: 窗口里既有占位符,也留着最后的 'done' 原文")

# ---- 6. force:无视阈值,能归档的全归档 ----
archive.clear()
context.reset()
setup_sys(cf())   # 大预算:压力必在 SAFE 之下
context.append_user("go")
context.append_assistant([{"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}}])
context.append_results([{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 1500}])
context.append_assistant([{"type": "text", "text": "mid"}])
context.append_assistant([{"type": "text", "text": "tail"}])

compact.compact(None)
assert archive.size() == 0
ok("force: 阈值内 compact 观察即收,不动")

compact.force(None)
assert archive.size() == 2   # tool 回合 + "mid" 文本回合都被归档;tail 保留
ok("force: 无视阈值,能归档的全归档,tail 仍保留")

# ---- 7. reactive:prompt_too_long 救一回、重试一次 ----
archive.clear()
context.reset()
setup_sys(cf())
context.append_user("go")
context.append_assistant([{"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}}])
context.append_results([{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 1500}])
context.append_assistant([{"type": "text", "text": "mid"}])
context.append_assistant([{"type": "text", "text": "tail"}])

hooks.USER_PROMPT_SUBMIT[:] = []
hooks.PRE_TOOL_USE[:] = [permission.check, plan.check]
hooks.POST_TOOL_USE[:] = []
hooks.STOP[:] = []


class TooLongThenOk(Model):
    def __init__(self):
        self.calls = 0

    def complete(self, messages, system, tools, max_tokens=8000):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("Error: prompt is too long (simulated)")
        return {"content": [{"type": "text", "text": "recovered"}],
                "stop_reason": "end_turn", "usage": {"input": 10, "output": 5}}


m = TooLongThenOk()
answer, _ = loop.run(m, "sys")
assert m.calls == 2 and answer == "recovered"
ok("reactive: 一次 prompt_too_long → force 压一轮 → 重试成功")
assert archive.size() == 2
ok("reactive: 救急时把老回合归档了")


# ---- 8. compact 工具:模型主动喊压,批处理完才压 ----
def tool_use(i, name, inp):
    return {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}],
            "stop_reason": "tool_use", "usage": {"input": 10, "output": 5}}


def end_turn(text):
    return {"content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "usage": {"input": 20, "output": 5}}


class ScriptModel(Model):
    def __init__(self, script):
        self.script = list(script)

    def complete(self, messages, system, tools, max_tokens=8000):
        return self.script.pop(0)


ANSWERS = iter([])


def fake_input(prompt=""):
    return next(ANSWERS)  # 意外提问 → StopIteration 当场炸


builtins.input = fake_input
permission._GRANTS.clear()
plan._APPROVED = None

assert "compact" in tools.REGISTRY and permission.check("compact", {}) is None
ok("compact 工具: 已注册且免问放行")

archive.clear()
context.reset()
setup_sys(cf())
context.append_user("phase two: compact the previous phase")   # pin
context.append_assistant([{"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}}])
context.append_results([{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 1500}])
context.append_assistant([{"type": "text", "text": "phase one done"}])

answer, _ = loop.run(
    ScriptModel([tool_use("t2", "compact", {}), end_turn("ready for phase two")]),
    "sys",
)
assert answer == "ready for phase two"
assert archive.size() == 2   # 上一阶段的 tool 回合 + "phase one done" 文本回合被归档
ok("compact 工具: 模型喊压 → 整批跑完后 force 归档上一阶段")

msgs = context.emit()
assert msgs[0]["role"] == "user"
assert any(isinstance(m["content"], str) and "[归档]" in m["content"] for m in msgs)
tr = [b for m in msgs if isinstance(m.get("content"), list)
      for b in m["content"] if b.get("type") == "tool_result"]
assert tr and "Compaction requested" in tr[0]["content"] and tr[0].get("is_error") is not True
ok("compact 工具: 确认回执不报错,窗口仍留本阶段原话 + 占位")

print(f"\nALL {PASS} assertions passed")