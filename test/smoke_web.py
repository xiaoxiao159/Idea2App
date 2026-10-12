"""Idea2App web 层冒烟:bootstrap 装配 + bridge 裁决桥 + handle_turn 一轮完整流转。不打真 API、不起 HTTP。

验证的是连接的核心:模型写 PRD.md → 发 gate_request + doc(draft) → 浏览器裁决放行 →
phase prd_approved + doc(approved) → on_stop 自动冲进 SPEC → 再拦一次 → 放行 → doc(frozen)
→ 一轮结束回 assistant + usage。

跑法(仓库根):python test/smoke_web.py
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 0. 隔离:必须在 import agent 之前 chdir(WORKDIR gotcha)。
tmp = Path(tempfile.mkdtemp())
os.chdir(tmp)

from agent.config import Config  # noqa: E402
from agent.models.base import Model  # noqa: E402
from Idea2App import bootstrap  # noqa: E402
from Idea2App.phase import Stage  # noqa: E402
from Idea2App.settings import Settings  # noqa: E402
from backend import bridge  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


events: list[dict] = []


def emit(type_, **data) -> None:
    events.append({"type": type_, **data})


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


# 装配(把本轮脚本注入;load_config/get_model 换成假的,其余走真 setup 路径)
script = FakeModel([
    w("t1", "PRD.md", "draft one"),      # 拦 PRD → 裁决放行 → 落盘
    e("PRD done"),                       # on_stop → auto SPEC_DRAFT → continue
    w("t2", "SPEC.md", "draft two"),     # 拦 SPEC → 放行 → 落盘 + 冻结
    e("SPEC done, frozen"),              # SPEC_APPROVED → 交还控制权
])
bootstrap.get_model = lambda config: script
bootstrap.load_config = lambda: Config("fake", "fake", "", "", window=8000, max_tokens=8000)

skills_dir = tmp / "skills"
skills_dir.mkdir()
settings = Settings(skills_dir=skills_dir, market_file=skills_dir / "market.json",
                    project_dir=tmp, project_skills_dir=tmp / "ps")

session = bridge.Session(emit)
app = bootstrap.setup(
    settings=settings,
    ask=session.ask,
    pre_hooks=[bridge.make_pre_tool(emit)],
    post_hooks=[bridge.make_post_tool(emit, session)],
)
session.store = app.store
bridge.wrap_transitions(app.store, emit)
assert app.store.current == Stage.IDEA; ok("setup: 起点 idea")


def resolver():
    for _ in range(2):  # PRD 一次 + SPEC 一次
        v = session.wait_pending()
        v.resolve(True, "")


t = threading.Thread(target=bridge.handle_turn,
                     args=(app, session, emit, {"type": "user", "text": "make a ledger app"}))
r = threading.Thread(target=resolver)
t.start(); r.start()
t.join(30); r.join(5)
assert not t.is_alive(), "worker 未在超时内跑完(裁决桥 deadlock?)"

# ---- 校验事件序 ----
types = [x["type"] for x in events]

gates = [x for x in events if x["type"] == "gate_request"]
assert [g["kind"] for g in gates] == ["prd", "spec"], f"gate 顺序错: {[g['kind'] for g in gates]}"
ok("gate_request 顺序 prd → spec")
assert gates[0]["content"] == "draft one" and gates[1]["content"] == "draft two"; ok("gate_request 带全文")

phases = [x["stage"] for x in events if x["type"] == "phase"]
for wanted in ("prd_draft", "prd_approved", "spec_draft", "spec_approved"):
    assert wanted in phases, f"缺 phase {wanted}: {phases}"
ok("phase 全链路 prd_draft→…→spec_approved")
assert phases.index("prd_approved") < phases.index("spec_draft") < phases.index("spec_approved"); ok("phase 顺序正确")

docs = {(x["kind"], x["status"]): x for x in events if x["type"] == "doc"}
assert docs[("prd", "draft")]["content"] == "draft one"; ok("doc prd.draft 先到")
assert docs[("prd", "approved")]["content"] == "draft one"; ok("doc prd.approved 落盘后")
assert docs[("spec", "frozen")]["content"] == "draft two"; ok("doc spec.frozen")

tool_calls = [x for x in events if x["type"] == "tool_call"]
assert [x["name"] for x in tool_calls] == ["write", "write"]; ok("tool_call 观测到两次 write")

assist = [x for x in events if x["type"] == "assistant"]
assert assist and assist[-1]["text"] == "SPEC done, frozen"; ok("assistant 回传一轮终答")
assert any(x["type"] == "usage" and x["input"] for x in events); ok("usage 回传 token")

# ok 的 tool_result 出现两次;PRD.md/SPEC.md 都真正落盘
results = [x for x in events if x["type"] == "tool_result"]
assert all(x["ok"] for x in results) and len(results) == 2; ok("tool_result 两次成功")
assert (tmp / "PRD.md").read_text(encoding="utf-8") == "draft one"; ok("PRD.md 落盘")
assert (tmp / "SPEC.md").read_text(encoding="utf-8") == "draft two"; ok("SPEC.md 落盘")

print(f"\nALL {PASS} assertions passed")