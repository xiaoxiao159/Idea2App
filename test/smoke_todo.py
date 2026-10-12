"""todo_write + 计划闸门冒烟:校验 / 全量替换语义 / diff 摘要 / 闸门粒度 / loop 集成。"""
import builtins
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import context, hooks, loop, tools  # noqa: E402
from agent.hooks import audit, permission, plan  # noqa: E402
from agent.models.base import Model  # noqa: E402
from agent.tools import todo  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


os.chdir(tempfile.mkdtemp())  # audit 日志落在临时目录

ANSWERS = iter([])


def fake_input(prompt=""):
    return next(ANSWERS)  # 没排到的提问 → StopIteration 当场炸,测试不撒谎


builtins.input = fake_input


def plan_items(*pairs):
    return [{"content": c, "status": s} for c, s in pairs]


def tool_results(messages):
    return [b for m in messages if isinstance(m.get("content"), list)
            for b in m["content"] if b.get("type") == "tool_result"]


# ---- 1. validate:非法提案止步于门口 ----
assert todo.validate(None) is not None; ok("validate: 非数组报错")
assert todo.validate("x") is not None; ok("validate: 字符串报错")
assert todo.validate([{"content": "a"}]) is not None; ok("validate: 非对象条目报错")
assert todo.validate([]) is None; ok("validate: 空清单合法(= 清空)")
assert todo.validate(plan_items(("a", "bogus"))) is not None; ok("validate: 非法 status")
assert todo.validate(plan_items(("", "pending"))) is not None; ok("validate: 空 content")
assert todo.validate(plan_items(("a", "pending"), ("a", "pending"))) is not None; ok("validate: 重复 content")
assert todo.validate(plan_items(("a", "in_progress"), ("b", "in_progress"))) is not None
ok("validate: 双 in_progress")
assert todo.validate([{"content": f"t{i}", "status": "pending"} for i in range(21)]) is not None
ok("validate: 超 20 条")
assert todo.validate(plan_items(("a", "pending"), ("b", "in_progress"))) is None; ok("validate: 合法清单")

# ---- 2. handler:全量替换 + diff 摘要回显 ----
todo._TODOS = []

text, err = tools.run_tool("todo_write", {"todos": plan_items(("a", "pending"), ("b", "in_progress"))})
assert err is False and "[~] b" in text and "+2 added" in text; ok("首写:回显清单 + added 摘要")

text, err = tools.run_tool("todo_write", {"todos": plan_items(("a", "completed"), ("b", "in_progress"))})
assert err is False and "~1 status change" in text; ok("只改状态:status change 摘要")

text, err = tools.run_tool(
    "todo_write",
    {"todos": plan_items(("a", "completed"), ("b", "completed"), ("c", "pending"))},
)
assert err is False and "+1 added" in text and "(2/3 done)" in text; ok("加条目 + 进度统计")

text, err = tools.run_tool("todo_write", {"todos": plan_items(("a", "completed"))})
assert err is False and "-2 removed" in text; ok("删条目")

text, err = tools.run_tool("todo_write", {"todos": plan_items(("a", "completed"))})
assert err is False and "identical to previous list" in text; ok("幂等重写:同集合同状态")

text, err = tools.run_tool("todo_write", {"todos": []})
assert err is False and "cleared" in text; ok("清空合法")

text, err = tools.run_tool("todo_write", {"todos": plan_items(("a", "in_progress"), ("b", "in_progress"))})
assert err is True; ok("handler 侧校验照样把关(不依赖闸门在场)")

s = tools.summary("todo_write", {"todos": plan_items(("a", "completed"), ("b", "in_progress"))})
assert "todo_write" in s and "1 done" in s and "1 in progress" in s; ok("display 摘要")
s = tools.summary("todo_write", {"todos": "garbage"})
assert "invalid" in s; ok("display 对垃圾输入不炸(display 在校验前跑)")

# ---- 3. plan.check:内容级闸门 ----
plan._APPROVED = None
todo._TODOS = []

ANSWERS = iter([])
assert plan.check("bash", {}) is None; ok("非 todo_write 不发言(不消耗输入)")

ANSWERS = iter(["y"])
assert plan.check("todo_write", {"todos": plan_items(("a", "pending"))}) is None
ok("首份计划:问一次,y 放行")

ANSWERS = iter([])
assert plan.check("todo_write", {"todos": plan_items(("a", "completed"))}) is None
ok("批准后只改状态:免问")

ANSWERS = iter([])
assert plan.check("todo_write", {"todos": plan_items(("a", "in_progress"), ("b", "bogus"))}) is not None
ok("非法清单:不问人,直接退回")

ANSWERS = iter(["y"])
assert plan.check("todo_write", {"todos": plan_items(("a", "pending"), ("b", "pending"))}) is None
ok("工作集合变了:再问")

ANSWERS = iter(["n", "scope too big"])
v = plan.check("todo_write", {"todos": plan_items(("a", "pending"), ("b", "pending"), ("c", "pending"))})
assert v is not None and "rejected" in v and "scope too big" in v and "todo_write again" in v
ok("n + 理由:否决串带上理由和返工指令")

ANSWERS = iter([])
assert plan.check("todo_write", {"todos": plan_items(("a", "pending"), ("b", "pending"))}) is None
ok("退回已批集合:免问(工作集合没变)")

plan._APPROVED = None
ANSWERS = iter([])
assert plan.check("todo_write", {"todos": []}) is None
ok("空对空(还没批过任何东西):免问")

plan._APPROVED = {"a"}
ANSWERS = iter(["n", "keep it"])
assert plan.check("todo_write", {"todos": []}) is not None
ok("清空已批计划 = 工作集合变更:要问")

# ---- 4. permission:todo_write 属于 ALLOW,权限闸门不提问 ----
permission._GRANTS.clear()
ANSWERS = iter([])
assert permission.check("todo_write", {"todos": []}) is None; ok("permission 对 todo_write 直接放行")

# ---- 5. loop 集成:两级闸门(permission 放行 → plan 问人)全链路 ----
hooks.USER_PROMPT_SUBMIT[:] = [audit.on_prompt]
hooks.PRE_TOOL_USE[:] = [permission.check, plan.check]
hooks.POST_TOOL_USE[:] = [audit.on_tool]
hooks.STOP[:] = [audit.on_stop]
permission._GRANTS.clear()
plan._APPROVED = None
todo._TODOS = []

class FakeModel(Model):
    """按剧本回放统一响应:loop 只依赖 complete 这一个面。"""

    def __init__(self, script):
        self.script = list(script)

    def complete(self, messages, system, tools, max_tokens=8000):
        return self.script.pop(0)


def tool_use(i, name, inp):
    return {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}],
            "stop_reason": "tool_use", "usage": {"input": 10, "output": 5}}


def end_turn(text):
    return {"content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "usage": {"input": 20, "output": 5}}


# 5a. y 批准 → tool_result 回显清单
context.reset()
context.append_user("make a plan")
ANSWERS = iter(["y"])
answer, _ = loop.run(
    FakeModel([
        tool_use("t1", "todo_write",
                 {"todos": plan_items(("step1", "pending"), ("step2", "pending"))}),
        end_turn("plan approved, starting work"),
    ]),
    "sys",
)
tr = tool_results(context.emit())
assert "starting work" in answer
assert tr[0].get("is_error") is not True and "[ ] step1" in tr[0]["content"]
ok("集成:y 批准 → tool_result 回显清单,非错误")

# 5b. n+理由 → is_error 回给模型 → 模型改清单重发 → y 放行
context.reset()
context.append_user("make a plan")
ANSWERS = iter(["n", "too many steps", "y"])
answer, _ = loop.run(
    FakeModel([
        tool_use("t1", "todo_write",
                 {"todos": plan_items(("step1", "pending"), ("step2", "pending"),
                                      ("step3", "pending"), ("step4", "pending"))}),
        # 收到否决:砍掉 step4,重发
        tool_use("t2", "todo_write",
                 {"todos": plan_items(("step1", "pending"), ("step2", "pending"),
                                      ("step3", "pending"))}),
        end_turn("revised plan approved"),
    ]),
    "sys",
)
tr = tool_results(context.emit())
assert "revised plan approved" in answer
assert tr[0].get("is_error") is True and "too many steps" in tr[0]["content"]
ok("集成:n+理由 → is_error 回给模型,模型改清单重发")
assert tr[1].get("is_error") is not True and "[ ] step1" in tr[1]["content"]
ok("集成:修订版 y → 放行")
assert plan._APPROVED == {"step1", "step2", "step3"}; ok("集成:批准集合更新为修订版")
assert len(todo._TODOS) == 3; ok("集成:_TODOS 已是修订版(全量替换)")

print(f"\nALL {PASS} assertions passed")
