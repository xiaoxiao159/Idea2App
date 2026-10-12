"""team (s13) 冒烟:命名/信箱/计划闸门/停机协议/常驻队友跑完全程(fake model,不打 API)。"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())

from agent import team  # noqa: E402
from agent.models.base import Model  # noqa: E402
from agent.tasks import store  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


def reset_team():
    team.active_teammates.clear()
    team.plan_gates.clear()
    team.plan_request_ids.clear()
    team.pending_requests.clear()
    store.teammate_assignments.clear()
    store.assignment_versions.clear()


# ---- 1. 命名与保留名 ----
assert team.is_valid_agent_name("alice") is True
assert team.is_valid_agent_name("ok-name_1") is True
assert team.is_valid_agent_name("bad name") is False
assert team.is_valid_agent_name("") is False
assert team.is_valid_agent_name("x" * 65) is False
assert team.spawn_teammate_thread("lead", "role", "p").startswith("Invalid teammate name: 'lead'")
assert team.spawn_teammate_thread("bad name", "role", "p").startswith("Invalid teammate name: ")
ok("命名:合法/超长/含空格 判定 + lead/agent 保留名拒绝")

# ---- 2. MessageBus:破坏性读 + peek + wait ----
reset_team()
team.BUS.send("lead", "alice", "hello")
team.BUS.send("lead", "bob", "yo")
box = team.BUS.read_inbox("alice")
assert len(box) == 1 and box[0]["content"] == "hello" and box[0]["from"] == "lead"
assert team.BUS.read_inbox("alice") == []  # 破坏性
assert team.BUS.peek("bob") is True
assert team.BUS.read_inbox("bob")[0]["content"] == "yo"
assert team.BUS.peek("bob") is False
assert team.BUS.read_inbox("bob") == []
team.BUS.send("lead", "carol", "later")
todo = team.BUS.wait_for_messages("carol", timeout=0.5)
assert len(todo) == 1 and todo[0]["content"] == "later"
ok("MessageBus: 破坏性读 / peek / wait_for_messages")

# ---- 3. 计划闸门:归档 → 拒绝 → 重新提交 → 批准(纯协议)----
reset_team()
team.active_teammates["alice"] = "working"
team.plan_gates["alice"] = "required"
store.assignment_versions["alice"] = 0
store.teammate_assignments["alice"] = {"task_id": "task_deadbeef"}

text = team._teammate_submit_plan("alice", "step 1")
assert text.startswith("Plan submitted (")
assert team.plan_gates["alice"] == "pending"
rid = team.plan_request_ids["alice"]
assert team.pending_requests[rid].status == "pending"
ok("submit_plan: 建 pending 请求 + 队友转 waiting(记录在册)")

assert team.run_review_plan(rid, approve=False, feedback="redo") == f"Plan rejected ({rid})"
resp = next(m for m in team.BUS.read_inbox("alice") if m["type"] == "plan_approval_response")
accepted, notice = team.apply_plan_response("alice", resp)
assert accepted is True and "rejected" in notice
assert team.plan_gates["alice"] == "rejected"
ok("review_plan(拒绝) → apply_plan_response: 闸门落 rejected")

accepted, notice = team.apply_plan_response("alice", resp)  # 旧响应重放
assert accepted is False and "mismatch" in notice
ok("apply_plan_response: 旧 request_id 重放被拒")

text = team._teammate_submit_plan("alice", "step 2")
rid2 = team.plan_request_ids["alice"]
assert team.run_review_plan(rid2, approve=True).startswith("Plan approved")
resp2 = next(m for m in team.BUS.read_inbox("alice") if m["type"] == "plan_approval_response")
accepted, notice = team.apply_plan_response("alice", resp2)
assert accepted and "approved" in notice and team.plan_gates["alice"] == "approved"
ok("重新提交 + 批准: 闸门落 approved")

# ---- 4. 停机协议(request → 响应 → match_response)----
text = team.run_request_shutdown("alice")
assert text.startswith("Shutdown requested from alice")
sreq = next(s for s in team.pending_requests.values() if s.type == "shutdown")
smsg = next(m for m in team.BUS.read_inbox("alice") if m["type"] == "shutdown_request")
accepted, rid_s = team.apply_shutdown_request("alice", smsg)
assert accepted is True and rid_s == sreq.request_id
assert team.active_teammates["alice"] == "stopping"
team.BUS.send("alice", "lead", "Shutdown acknowledged.", "shutdown_response",
              {"request_id": rid_s, "approve": True})
team.consume_lead_inbox()
assert team.pending_requests[rid_s].status == "approved"
ok("shutdown: 请求/回应/状态机 + match_response 消化")

# ---- 5. 常驻队友跑完全程(fake model)----
reset_team()
task = store.TASKS.create("build the widget", "assemble parts")


class FakeTeamModel(Model):
    def __init__(self, script):
        self.script = list(script)

    def complete(self, messages, system, tools, max_tokens=8000):
        return {"content": self.script.pop(0)}


def tool_use(block_id, name, inp):
    return {"type": "tool_use", "id": block_id, "name": name, "input": inp}


def text(t):
    return {"type": "text", "text": t}


team.set_model(FakeTeamModel([
    [tool_use("tu1", "complete_task", {"task_id": task.id})],
    [text("built and shipped it")],
]))

out = team.spawn_teammate_thread("builder", "builder", "assemble it", task_id=task.id)
assert out.startswith("Teammate 'builder' spawned")

deadline = time.time() + 8
while time.time() < deadline and team.active_teammates.get("builder") != "idle":
    time.sleep(0.05)
assert team.active_teammates.get("builder") == "idle"

assert store.TASKS.load(task.id).status == "completed"
assert "builder" not in store.teammate_assignments
msgs = team.consume_lead_inbox()
types = {m["type"] for m in msgs}
assert "result" in types and "idle_notification" in types
result = next(m for m in msgs if m["type"] == "result")
assert "built and shipped" in result["content"]
ok("队友跑全程: 认领→complete_task→释放→result/idle 事件投递")

team.set_model(None)

print(f"\nALL {PASS} assertions passed")