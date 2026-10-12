"""subagent 冒烟:task 在册 / 子工具集收窄 / 只还最终文本 / 经 tools.run_tool 全链路。

不碰真实 API:FakeModel 按剧本回放统一响应。父循环用自己的 model,
子循环读模块级 CURRENT_MODEL(subagent.set_model 设的那个)——和真运行同构。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import context, hooks, loop, subagent, tools  # noqa: E402
from agent.models.base import Model  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


os.chdir(tempfile.mkdtemp())


def tool_results(messages):
    return [b for m in messages if isinstance(m.get("content"), list)
            for b in m["content"] if b.get("type") == "tool_result"]


# ---- 1. 工具集形状 ----
names = {t["name"] for t in tools.api_tools()}
assert "task" in names; ok("task 工具进了父模型 api_tools()")

sub_names = {t["name"] for t in subagent._sub_api_tools()}
assert sub_names == {"bash", "read", "write", "edit"}; ok("子工具集 = 四件基础工具,无 task/todo_write/skill/compact")


class FakeModel(Model):
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


hooks.PRE_TOOL_USE[:] = []
hooks.POST_TOOL_USE[:] = []
hooks.STOP[:] = []

# ---- 2. 没 set_model 就调 task → handler 诚实报错 ----
subagent.CURRENT_MODEL = None
text, err = tools.run_tool("task", {"prompt": "hi"})
assert err is True and "not set" in text; ok("没 set_model 就调 task → 诚实报错,不装干成")

# ---- 3. 经 tools.run_tool 走 handler:只还最终文本 ----
subagent.set_model(FakeModel([
    tool_use("s1", "read", {"file_path": "no_such.py"}),
    end_turn("the repo does X"),
]), 8000)

text, err = tools.run_tool("task", {"prompt": "summarize this repo"})
assert err is False and text == "the repo does X"; ok("tools.run_tool('task'):子循环跑 1 个工具后只还最终文本")

text, err = tools.run_tool("task", {"prompt": ""})
assert err is True and "non-empty" in text; ok("空 prompt(经 handler 同样走到校验)→ is_error")

# ---- 4. loop 集成:父 loop 调 task → 父窗口只收最终文本,不泄漏子循环 ----
subagent.set_model(FakeModel([
    tool_use("s1", "read", {"file_path": "no_such.py"}),
    end_turn("sub final answer"),
]), 8000)

context.reset()
context.append_user("delegate a lookup")
answer, _ = loop.run(
    FakeModel([
        tool_use("p1", "task", {"prompt": "find the answer"}),
        end_turn("I delegated and got the answer"),
    ]),
    "sys",
)
assert "I delegated and got" in answer; ok("集成:父 agent 用 task 委派后正常收工")

tr = tool_results(context.emit())
assert len(tr) == 1; ok("集成:父窗口只剩 1 条 tool_result(子循环的中间往返一个没进)")
assert tr[0]["content"] == "sub final answer" and tr[0].get("is_error") is not True
ok("集成:task 的 tool_result 只含子循环最终文本,非错误")
assert "no_such.py" not in str(context.emit())
ok("集成:子循环的内部往返(读 no_such.py)没有泄漏进父窗口")

print(f"\nALL {PASS} assertions passed")