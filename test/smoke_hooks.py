"""hooks + permission 冒烟:空表回归 / 权限全链路 / fire 语义 / loop 集成 / audit 落盘。"""
import builtins
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import context, hooks, loop  # noqa: E402
from agent.hooks import audit, permission  # noqa: E402
from agent.models.base import Model  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


os.chdir(tempfile.mkdtemp())  # audit 日志和 ls 落在临时目录

# ---- 1. 空钩子回归:四表清空,fire_* 全直通(= s02 行为) ----
hooks.USER_PROMPT_SUBMIT.clear()
hooks.PRE_TOOL_USE.clear()
hooks.POST_TOOL_USE.clear()
hooks.STOP.clear()

assert hooks.fire_submit("hello") == "hello"; ok("empty: submit 直通")
assert hooks.fire_pre("bash", {"command": "ls"}) is None; ok("empty: pre 直通")
hooks.fire_post("bash", {}, "x", False); ok("empty: post 不炸")
assert hooks.fire_stop("done", {}) is None; ok("empty: stop 直通")

# ---- 2. 权限全链路 ----
ANSWERS = iter([])


def fake_input(prompt=""):
    return next(ANSWERS)  # 没排到的提问 → StopIteration 当场炸,测试不撒谎


builtins.input = fake_input

assert permission.check("read", {"file_path": "x"}) is None; ok("read 放行不提问")
assert permission.check("nope", {}) is None; ok("幻觉工具放行(交给 unknown tool 报错)")

permission._GRANTS.add("bash:ls")
v = permission.check("bash", {"command": "ls && rm -rf /"})
assert v is not None and "deny floor" in v; ok("deny floor 命中")
assert permission.check("bash", {"command": "sudo apt install x"}) is not None; ok("deny floor: sudo")
assert permission.check("bash", {"command": "ls && rm -rf /"}) is not None; ok("deny floor 压过 a 授权")
permission._GRANTS.clear()

ANSWERS = iter(["n"])
v = permission.check("bash", {"command": "ls"})
assert v is not None and "user denied" in v and "Do not retry" in v; ok("n 否决 + 措辞封死重试")
assert "bash:ls" not in permission._GRANTS; ok("n 不留授权")

ANSWERS = iter(["a"])
assert permission.check("bash", {"command": "ls"}) is None; ok("a 放行")
ANSWERS = iter([])
assert permission.check("bash", {"command": "ls -la"}) is None; ok("a 记住程序名,免问")

ANSWERS = iter(["y"])
assert permission.check("bash", {"command": "cat x"}) is None; ok("y 本次放行")
assert "bash:cat" not in permission._GRANTS; ok("y 不留授权")

ANSWERS = iter([""])
assert permission.check("bash", {"command": "python x.py"}) is not None; ok("回车默认 n")

ANSWERS = iter([])
assert permission.check("bash", {"command": ""}) is None; ok("空命令放行给 handler")

ANSWERS = iter(["a"])
assert permission.check("write", {"file_path": "a.txt", "content": "x"}) is None; ok("write a 放行")
ANSWERS = iter([])
assert permission.check("write", {"file_path": "b.txt", "content": "y"}) is None; ok("write 授权按工具名记住")

# ---- 3. fire 语义:gate / transform / observe + 故障方向 ----
ran = []


def d1(n, p):
    ran.append("d1")
    return "no way"


def d2(n, p):
    ran.append("d2")
    return None


hooks.PRE_TOOL_USE[:] = [d1, d2]
assert hooks.fire_pre("bash", {}) == "no way"; ok("gate 首个非 None 终审")
assert ran == ["d1"]; ok("gate 短路后续钩子")


def boom(*args):
    raise RuntimeError("boom")


hooks.PRE_TOOL_USE[:] = [boom]
v = hooks.fire_pre("bash", {})
assert v is not None and "boom" in v; ok("pre 崩溃 = 否决(故障关闭)")

hooks.POST_TOOL_USE[:] = [boom]
buf = io.StringIO()
with redirect_stdout(buf):
    hooks.fire_post("bash", {}, "x", False)
assert "warn" in buf.getvalue(); ok("post 崩溃 = 旁路")

hooks.STOP[:] = [boom]
with redirect_stdout(buf):
    assert hooks.fire_stop("ans", {}) is None
ok("stop 崩溃 = 旁路")


def add_a(q):
    return q + "a"


def watch(q):
    ran.append(q)
    return None


hooks.USER_PROMPT_SUBMIT[:] = [add_a, watch]
assert hooks.fire_submit("q") == "qa"; ok("submit 链式改写")
assert ran[-1] == "qa"; ok("后一个钩子看到改写结果")

hooks.USER_PROMPT_SUBMIT[:] = [lambda q: ""]
assert hooks.fire_submit("orig") == "orig"; ok("空串改写视同 None")

# ---- 4. loop 集成(fake model,不打真 API) ----


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


def ls_script():
    return FakeModel([tool_use("t1", "bash", {"command": "ls"}), end_turn("ok done")])


def tool_results(messages):
    return [b for m in messages if isinstance(m.get("content"), list)
            for b in m["content"] if b.get("type") == "tool_result"]


# 4a. 空表下 loop 直跑工具(= s02 回归):若有人提问,StopIteration 当场炸
hooks.USER_PROMPT_SUBMIT.clear()
hooks.PRE_TOOL_USE.clear()
hooks.POST_TOOL_USE.clear()
hooks.STOP.clear()
context.reset()
context.append_user("ls without hooks")
ANSWERS = iter([])
answer, _ = loop.run(ls_script(), "sys")
assert "ok done" in answer and "is_error" not in tool_results(context.emit())[-1]
ok("空表:loop 直跑工具,无闸门(= s02 行为)")

# 4b. 恢复真实装配,权限全链路进 loop
hooks.USER_PROMPT_SUBMIT[:] = [audit.on_prompt]
hooks.PRE_TOOL_USE[:] = [permission.check]
hooks.POST_TOOL_USE[:] = [audit.on_tool]
hooks.STOP[:] = [audit.on_stop]
permission._GRANTS.clear()

context.reset()
context.append_user("run ls")
ANSWERS = iter(["n"])
answer, _ = loop.run(ls_script(), "sys")
tr = tool_results(context.emit())
assert "ok done" in answer; ok("集成:拒绝后模型收到错误,仍完成任务")
assert tr[0].get("is_error") is True and "user denied" in tr[0]["content"]
ok("集成:否决 → is_error tool_result 回给模型")
assert "bash:ls" not in permission._GRANTS; ok("集成:n 不留授权")

context.reset()
context.append_user("run ls again")
ANSWERS = iter(["a"])
answer, _ = loop.run(ls_script(), "sys")
tr = tool_results(context.emit())
assert "ok done" in answer and "is_error" not in tr[-1]; ok("集成:a 放行,真执行,非错误")
assert "bash:ls" in permission._GRANTS; ok("集成:a 记住")

# 4c. Stop 驳回续跑
vetoed = {"used": False}


def stop_veto(answer, usage):
    if not vetoed["used"]:
        vetoed["used"] = True
        return "not so fast"
    return None


hooks.STOP[:] = [stop_veto]
context.reset()
context.append_user("task")
ANSWERS = iter([])
answer, _ = loop.run(
    FakeModel([end_turn("i am done"), end_turn("really done")]), "sys")
assert answer == "really done"; ok("Stop 驳回:循环继续")
assert any(m["role"] == "user" and m["content"] == "not so fast" for m in context.emit())
ok("Stop 驳回:理由注入为 user 消息")

# ---- 5. audit 落盘 ----
hooks.STOP[:] = [audit.on_stop]
hooks.fire_submit("smoke query")
log = open("logs/session.log", encoding="utf-8").read()
assert "\tquery\t" in log; ok("audit: query 行")
assert "\tERR\t" in log and "user denied" in log; ok("audit: ERR 行(含被否决的调用)")
assert "\tok\t" in log; ok("audit: ok 行")
assert "\tstop\t" in log; ok("audit: stop 行")

print(f"\nALL {PASS} assertions passed")
