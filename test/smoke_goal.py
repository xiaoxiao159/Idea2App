"""goal (s17) 冒烟:JSON 校验 / 对话转录 / 控制器状态机 / loop 集成 / 后台 defer。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import background, context, goal, hooks, loop  # noqa: E402
from agent.models.base import Model  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


os.chdir(tempfile.mkdtemp())  # 背景任务 / 落盘 / 日志都落在临时目录


# ---- 1. _parse_json_object:合法 / 围栏 / 形状错 ----
_d = goal._parse_json_object
r = _d('{"ok": true, "reason": "pytest exit 0", "impossible": false}')
assert r == {"ok": True, "reason": "pytest exit 0", "impossible": False}
ok("parse: 合法 JSON 三字段齐全")

r = _d('```json\n{"ok": false, "reason": "no exit code yet"}\n```')
assert r["ok"] is False and "no exit code" in r["reason"] and r["impossible"] is False
ok("parse: 代码围栏剥掉,缺 impossible 默认 False")

for bad in ('{"ok": true, "reason": "x", "impossible": true}',  # ok 与 impossible 冲突
            '{"ok": 1, "reason": "x"}',                           # ok 非布尔
            '{"ok": true}',                                       # 缺 reason
            '{"ok": true, "reason": "   "}',                      # reason 空白
            'not json',                                           # 坏 JSON
            '[1, 2]'):                                            # 非对象
    try:
        _d(bad)
        raise SystemExit(f"should have raised GoalError: {bad}")
    except goal.GoalError:
        pass
ok("parse: 六种坏形状全抛 GoalError")

# ---- 2. transcript_text:rolE 前缀 + 最新超大条掐头去尾 ----
msgs = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": [{"type": "text", "text": "doing"}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x" * 30}]},
]
t = goal.transcript_text(msgs)
assert "USER:\nhi" in t and "ASSISTANT:\ndoing" in t
ok("transcript: role 前缀 + 块拍平(工具结果内容在场)")

huge = "LONG" * 10000
t = goal.transcript_text([{"role": "user", "content": huge}], max_chars=100)
assert "[middle omitted]" in t and len(t) <= 110
ok("transcript: 最新一条超大 → 掐头去尾,不吞整窗")


# ---- 3. GoalController 状态机(fake evaluator) ----
class FakeEvaluator:
    def __init__(self, script):
        self.script = list(script)

    def evaluate(self, condition, messages):
        return self.script.pop(0)


def ev(ok_, reason, impossible=False):
    return goal.GoalEvaluation(ok=ok_, reason=reason, impossible=impossible)


c = goal.GoalController(evaluator=None)
assert c.evaluate_after_turn([]).action == "allow"
ok("controller: 无活动 goal → allow(不碰 evaluator)")

c.set_goal("pytest tests exits 0", tokens_at_start=100)
assert c.active is not None and c.active.condition == "pytest tests exits 0"
assert c.status(130).startswith("Goal active: pytest tests exits 0")
assert "Tokens: 30" in c.status(130) and "Evaluations: 0" in c.status(130)
ok("controller: set_goal 记条件 + status 报 token 增量")

assert c.evaluate_after_turn([], background_running=True).action == "defer"
assert c.active is not None and c.active.iterations == 0
ok("controller: 后台还在跑 → defer,evaluator 不判、计数器不动")

c.evaluator = FakeEvaluator([ev(False, "no test result yet")])
d = c.evaluate_after_turn([])
assert d.action == "block" and "no test result" in d.reason
assert c.active is not None and c.active.iterations == 1 and c.consecutive_blocks == 1
ok("controller: 断言未满足 → block,计数器 +1")

c.evaluator = FakeEvaluator([ev(True, "pytest exit 0")])
d = c.evaluate_after_turn([])
assert d.action == "achieved" and c.active is None and c.consecutive_blocks == 0
ok("controller: 满足 → achieved,goal 清空、连续 block 归零")

c2 = goal.GoalController(evaluator=FakeEvaluator([ev(False, "cannot be done", impossible=True)]))
c2.set_goal("build a time machine")
assert c2.evaluate_after_turn([]).action == "failed" and c2.active is None
ok("controller: impossible → failed,goal 结束")

c3 = goal.GoalController(evaluator=FakeEvaluator([ev(False, "keep going")] * 3), block_cap=2)
c3.set_goal("x")
assert c3.evaluate_after_turn([]).action == "block"
assert c3.evaluate_after_turn([]).action == "block"
assert c3.evaluate_after_turn([]).action == "limit"
assert c3.active is not None  # limit 不清 goal,交还控制权
ok("controller: 连续 block 超 block_cap → limit,goal 留着")

c3.begin_query()
assert c3.consecutive_blocks == 0
ok("controller: begin_query 清连续 block 计数")

c4 = goal.GoalController(evaluator=FakeEvaluator([]))
c4.set_goal("x")


def boom(cond, msgs):
    raise RuntimeError("evaluator down")


c4.evaluator = boom
assert c4.evaluate_after_turn([]).action == "error" and c4.active is not None
ok("controller: evaluator 崩 → error,goal 留着(不猜、不谎报成功)")

# status:达到/失败后的结论能回看
assert "Goal achieved" in c.status()
c5 = goal.GoalController(evaluator=None)
assert c5.status() == "No goal set"
c5.set_goal("z")
assert c5.clear().startswith("Goal cleared: z")
assert c5.status() == "No goal set" and c5.clear() == "No goal set"
ok("controller: status/clear 的空态与清除")


# set_goal 校验
def set_bad(cond):
    try:
        goal.GoalController(evaluator=None).set_goal(cond)
        raise SystemExit(f"should have raised: {cond!r}")
    except goal.GoalError:
        pass


set_bad("")
set_bad("   ")
set_bad("x" * (goal.MAX_GOAL_LENGTH + 1))
ok("controller: 空条件 / 超长条件 → GoalError")

# 替换旧 goal 记录事件
c6 = goal.GoalController(evaluator=None, events=[])
c6.set_goal("first")
c6.set_goal("second")
assert any(e["reason"] == "replaced by a new goal" for e in c6.events)
assert c6.active.condition == "second"
ok("controller: 新 goal 替换旧 goal 并记 replaced 事件")

# restore
ev2 = FakeEvaluator([])
c7 = goal.GoalController.restore(
    ev2,
    [{"type": "goal_status", "condition": "keep alive", "active": True,
      "met": False, "failed": False, "reason": "x"}],
)
assert c7.active is not None and c7.active.condition == "keep alive"
ok("controller: restore 从持久化事件找回仍活动的 goal")


# ---- 4. parse_command ----
assert goal.parse_command("hello") is None
assert goal.parse_command("/goal") == ""
assert goal.parse_command("/goal run tests") == "run tests"
assert goal.parse_command("/goal ") == ""
ok("parse_command: 三态分支(None / 空串 / 条件)")

# ---- 5. loop 集成(fake worker + fake evaluator,不打真 API) ----
hooks.PRE_TOOL_USE.clear()
hooks.POST_TOOL_USE.clear()
hooks.STOP.clear()  # 隔离掉 audit/permission,只验 goal 一条链路


class FakeWorker(Model):
    def __init__(self, script):
        self.script = list(script)

    def complete(self, messages, system, tools, max_tokens=8000):
        return self.script.pop(0)


def end_turn(text):
    return {"content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "usage": {"input": 20, "output": 5}}


goal.GOAL.evaluator = FakeEvaluator([
    ev(False, "pytest exit code not shown yet"),
    ev(True, "pytest tests/auth exits 0"),
])
goal.GOAL.set_goal("pytest tests/auth exits 0", tokens_at_start=0)
context.reset()
context.append_user("finish the auth tests")

answer, _ = loop.run(
    FakeWorker([end_turn("ran a few tests"), end_turn("all pass, pytest exit 0")]),
    "sys",
)
assert "all pass" in answer and "achieved" in answer
ok("loop: worker 停手未达标 → block 续轮,达标 → achieved 收工")
assert goal.GOAL.active is None, "达到后 goal 应清空"
ok("loop: 达标后 goal 清空")
assert any(
    isinstance(m.get("content"), str) and "Goal still active" in m["content"]
    for m in context.emit()
)
ok("loop: block 理由作为 user 消息灌回窗口([Goal still active])")

# 始终未达标 → 连续 block 撞 block_cap → limit,goal 留着交还控制权
class RepeatingWorker(Model):
    def __init__(self, response):
        self.response = response

    def complete(self, messages, system, tools, max_tokens=8000):
        return self.response


context.reset()
context.append_user("try again")
goal.GOAL.evaluator = FakeEvaluator([ev(False, "still no exit code")] * 10)
goal.GOAL.set_goal("pytest exits 0")
answer, _ = loop.run(RepeatingWorker(end_turn("done-ish")), "sys")
assert "[goal limit" in answer
assert goal.GOAL.active is not None, "limit 不清 goal"
ok("loop: 连续 block 撞 block_cap → limit,goal 留着交还控制权")
assert any("still no exit code" in str(m.get("content")) for m in context.emit())
ok("loop: 未达标 reason 逐次灌回窗口")

# ---- 6. background.any_running ----
background.BACKGROUND.tasks = {"bg_1": {"command": "x", "status": "running"}}
assert background.any_running() is True
background.BACKGROUND.tasks = {"bg_1": {"command": "x", "status": "completed"}}
assert background.any_running() is False
background.BACKGROUND.tasks = {}
ok("background: any_running 只认 status==running")

print(f"\nALL {PASS} assertions passed")