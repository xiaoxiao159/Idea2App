"""Idea2App web 后端传输层冒烟:FastAPI 静态托管 + WebSocket 事件流 + verdict 解阻塞。不打真 API。

用 fastapi TestClient(httpx ASGI transport)驱动 /ws:发 user → worker 线程跑引擎 →
收到 gate_request(PRD) → 发 verdict → dispatcher 逻辑解除 gate 阻塞 → 推进到 SPEC 关卡 →
再放行 → 收 assistant + usage。

跑法(仓库根):python test/smoke_server.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 0. 隔离:必须在 import agent 之前 chdir(WORKDIR gotcha)。
tmp = Path(tempfile.mkdtemp())
os.chdir(tmp)
os.environ["IDEA2APP_SKILLS_DIR"] = str(tmp / "skills")
(tmp / "skills").mkdir(exist_ok=True)

from agent.config import Config  # noqa: E402
from agent.models.base import Model  # noqa: E402
import backend.server as server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


def w(i, path, content):
    return {"content": [{"type": "tool_use", "id": i, "name": "write",
                         "input": {"file_path": path, "content": content}}],
            "stop_reason": "tool_use", "usage": {"input": 10, "output": 5}}


def e(text):
    return {"content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "usage": {"input": 20, "output": 5}}


class FakeModel(Model):
    def __init__(self, script):
        self.script = list(script)

    def complete(self, messages, system, tools, max_tokens=8000):
        return self.script.pop(0)


server.bootstrap.get_model = lambda config: FakeModel([
    w("t1", "PRD.md", "hello prd"),
    e("PRD done"),
    w("t2", "SPEC.md", "hello spec"),
    e("SPEC done"),
])
server.bootstrap.load_config = lambda: Config("fake", "fake", "", "", window=8000, max_tokens=8000)

client = TestClient(server.app)

# ---- 1. 静态托管 ----
r = client.get("/")
assert r.status_code == 200 and "Idea2App" in r.text and 'src="app.js"' in r.text
ok("GET / 出 index.html")
r = client.get("/styles.css")
assert r.status_code == 200 and "--accent" in r.text; ok("GET /styles.css")

# ---- 2. WebSocket 事件流 + verdict 解阻塞 ----
events: list[dict] = []
with client.websocket_connect("/ws") as ws:
    ev = ws.receive_json()
    events.append(ev)
    assert ev["type"] == "hello" and ev["stage"] == "idea"; ok("WS: hello")

    ws.send_json({"type": "user", "text": "make a ledger app"})
    while True:
        ev = ws.receive_json(); events.append(ev)
        if ev["type"] == "gate_request" and ev["kind"] == "prd":
            break
    assert ev["content"] == "hello prd"; ok("WS: PRD 关卡带全文")

    ws.send_json({"type": "verdict", "kind": "prd", "accept": True, "reason": ""})
    while True:
        ev = ws.receive_json(); events.append(ev)
        if ev["type"] == "gate_request" and ev["kind"] == "spec":
            break
    ok("WS: verdict 解阻塞 → 推进到 SPEC 关卡")

    phases = [x["stage"] for x in events if x["type"] == "phase"]
    assert "prd_approved" in phases and "spec_draft" in phases; ok("WS: phase 已推进")

    ws.send_json({"type": "verdict", "kind": "spec", "accept": True, "reason": ""})
    while True:
        ev = ws.receive_json(); events.append(ev)
        if ev["type"] == "usage":
            break
    assert any(x["type"] == "assistant" and x["text"] == "SPEC done" for x in events)
    ok("WS: 一轮结束回 assistant + usage")

docs = {(x["kind"], x["status"]) for x in events if x["type"] == "doc"}
assert ("prd", "approved") in docs and ("spec", "frozen") in docs; ok("WS: doc prd.approved + spec.frozen")

print(f"\nALL {PASS} assertions passed")