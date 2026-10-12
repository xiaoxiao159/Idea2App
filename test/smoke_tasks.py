"""task_system (s10) + background (s11) 冒烟:任务图 DAG/认领 + 后台执行/注入。"""
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import background, context, tools  # noqa: E402
from agent.tasks import store  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


os.chdir(tempfile.mkdtemp())
store.TASKS = store.TaskStore(Path.cwd() / ".tasks")


def run(name, params):
    return tools.run_tool(name, params)


def tid(text):
    return re.search(r"task_[0-9a-f]{8}", text).group()


# ---- 1. create / list / get ----
t1, e = run("create_task", {"subject": "setup schema", "description": "db tables"})
assert e is False and t1.startswith("created task_"), t1
id1 = tid(t1)
assert store.TASK_ID_PATTERN.fullmatch(id1), id1
assert (Path.cwd() / ".tasks" / f"{id1}.json").is_file()
ok("create_task: 返回匹配 ID + 落盘 .tasks/")

t2, _ = run("create_task", {"subject": "write api"})
id2 = tid(t2)
t3, _ = run("create_task", {"subject": "write tests"})
id3 = tid(t3)

text, e = run("list_tasks", {})
assert e is False and "[ ]" in text and "setup schema" in text and "write api" in text
ok("list_tasks: 三个 pending 节点摘要")

text, e = run("get_task", {"task_id": id1})
assert e is False and "setup schema" in text and "pending" in text
ok("get_task: 完整记录回显")

# ---- 2. 依赖:加边 / 去重 / 自环 / 缺边 / 环 ----
t, e = run("update_task", {"task_id": id2, "addBlockedBy": [id1]})
assert e is False and id1 in t
ok("update_task: 加依赖边")

t, e = run("update_task", {"task_id": id2, "addBlockedBy": [id1]})
assert e is False and id1 in t
ok("update_task: 重复加依赖不产生重边")

t, e = run("update_task", {"task_id": id1, "addBlockedBy": [id1]})
assert e is True and "itself" in t
ok("update_task: 自依赖被拒")

t, e = run("update_task", {"task_id": id1, "addBlockedBy": ["task_deadbeef"]})
assert e is True and "not found" in t
ok("update_task: 不存在的前置被拒")

t, e = run("update_task", {"task_id": id1, "addBlockedBy": [id2]})
assert e is True and "cycle" in t
ok("update_task: 环检测(schema 不能依赖 api,api 已依赖 schema)")

# ---- 3. claim / complete 状态机 ----
t, e = run("claim_task", {"task_id": id2})
assert e is True and "blocked by" in t and id1 in t
ok("claim_task: 依赖未完成 → 拒绝并报 blocked by")

t, e = run("complete_task", {"task_id": id3})
assert e is True and "pending" in t
ok("complete_task: pending 不能 complete")

t, e = run("claim_task", {"task_id": id1})
assert e is False and "claimed" in t
ok("claim_task: 无依赖任务认领成功")

t, e = run("complete_task", {"task_id": id1})
assert e is False and "completed" in t and "unblocked" in t and "write api" in t
ok("complete_task: 完成并显式报出解锁的下游(write api)")

# s13 语义:完成的认领保留到回合边界。这里手动 release,领导才能认领下一个。
assert store.release_completed_assignment("agent") is True
ok("release_completed_assignment: 认领完成即保留,回合边界才归还")

t, e = run("claim_task", {"task_id": id2})
assert e is False and "claimed" in t
ok("claim_task: 依赖满足后认领成功")

t, e = store.complete(id2, owner="someone-else")
assert e is True and "owned by agent" in t
ok("store.complete: owner 不匹配被拒")

# ---- 4. background ----
assert background.should_run_background("bash", {"command": "x", "run_in_background": True}) is True
assert background.should_run_background("bash", {"command": "x"}) is False
assert background.should_run_background("read", {"run_in_background": True}) is False
ok("should_run_background: 只有 bash + 显式 true 才走后台")

background.BACKGROUND.tasks = {"bg_0001": {"command": "echo hi", "status": "completed"}}
background.BACKGROUND.results = {"bg_0001": "hi"}
background.BACKGROUND._ready = ["bg_0001"]
notes = background.collect_background_results()
assert len(notes) == 1 and "<task_notification>" in notes[0] and "bg_0001" in notes[0]
ok("collect: <task_notification> 格式化 + 取空队列")

bg_id = background.start_background_task("echo smoke-bg")
assert bg_id.startswith("bg_")
got = []
for _ in range(100):
    got = background.collect_background_results()
    if got:
        break
    time.sleep(0.05)
assert got and "smoke-bg" in got[0] and "completed" in got[0], got
ok("后台真实执行:echo smoke-bg → 完成并收集")

# ---- 5. append_injected:并进上一条 user ----
context.reset()
context.append_results([{"type": "tool_result", "tool_use_id": "t", "content": "r"}])
context.append_injected("note1")
last = context.emit()[-1]
assert last["role"] == "user" and isinstance(last["content"], list)
assert last["content"][-1] == {"type": "text", "text": "note1"}
ok("append_injected: 并进上一条 user 的块列表,不新开消息")

context.reset()
context.append_user("hello")
context.append_injected("note2")
assert context.emit()[-1]["content"] == "hello\n\nnote2"
ok("append_injected: 上一条是字符串 → 拼接")

context.reset()
context.append_assistant([{"type": "text", "text": "hi"}])
context.append_injected("note3")
assert context.emit()[-1] == {"role": "user", "content": "note3"}
ok("append_injected: 上一条不是 user → 新开 user")

print(f"\nALL {PASS} assertions passed")