"""Idea2App 流程层冒烟:阶段机 / 安装器 / 文档审批门(决策 + loop 集成)。不打真 API。

跑法(仓库根):python test/smoke_idea2app.py
"""
import builtins
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 0. 隔离:必须在 import agent 之前 chdir —— agent/team.py 的 WORKDIR = 导入时的 Path.cwd()。
# 晚于导入再 chdir,write 工具仍落回仓库根(workspace.resolve_cwd 已被 team 钉死到 launch cwd)。
tmp = Path(tempfile.mkdtemp())
os.chdir(tmp)

from agent import context, hooks, loop  # noqa: E402
from agent.models.base import Model  # noqa: E402
from Idea2App import gate, install  # noqa: E402
from Idea2App.phase import PhaseError, PhaseStore, Stage  # noqa: E402
from Idea2App.settings import Settings  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


# ---- 1. 阶段机 ----
s = PhaseStore(tmp)
assert s.current == Stage.IDEA; ok("phase: 初始 idea")

for nxt in (Stage.PRD_DRAFT, Stage.PRD_APPROVED, Stage.SPEC_DRAFT,
            Stage.SPEC_APPROVED, Stage.EXECUTING, Stage.DONE):
    s.transition(nxt)
assert s.frozen; ok("phase: 全链路推进 + 冻结")

on_disk = json.loads((tmp / ".idea2app" / "phase.json").read_text(encoding="utf-8"))
assert on_disk["stage"] == "done"; ok("phase: 落盘")
assert PhaseStore(tmp).current == Stage.DONE; ok("phase: 重启续上")
s.reset()
assert s.current == Stage.IDEA; ok("phase: reset")

try:
    PhaseStore(tmp).transition(Stage.EXECUTING)  # IDEA 只能进 PRD_DRAFT
    assert False, "expected PhaseError"
except PhaseError:
    ok("phase: 非法迁移抛错")

s4 = PhaseStore(tmp)
s4.transition(Stage.PRD_DRAFT)
assert s4.artifact() == "PRD.md"
s4.transition(Stage.PRD_APPROVED)
assert s4.artifact() is None; ok("phase: 产物文件仅 draft 阶段有")

# ---- 2. 安装器(本地路径 / 市场名 / 坏 skill) ----
skills_root = tmp / "skills"


def make_skill(root, name):
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill {name}\n---\n\n# {name}\nbody\n",
        encoding="utf-8",
    )
    return d


st = Settings(skills_dir=skills_root, market_file=skills_root / "market.json",
              project_dir=tmp, project_skills_dir=tmp / "ps")

src = make_skill(tmp / "src", "demo-skill")
assert install.install_skill(str(src), st) == "demo-skill"; ok("install: 本地路径")
assert (skills_root / "demo-skill" / "SKILL.md").is_file(); ok("install: 落盘")

make_skill(tmp / "msrc", "market-skill")
(skills_root / "market.json").write_text(
    json.dumps({"prd": {"source": str(tmp / "msrc" / "market-skill"), "desc": "x"}}),
    encoding="utf-8",
)
assert install.install_skill("prd", st) == "market-skill"; ok("install: 市场名")

bad = tmp / "bad"
bad.mkdir()
(bad / "SKILL.md").write_text("no frontmatter\n", encoding="utf-8")
try:
    install.install_skill(str(bad), st)
    assert False, "expected InstallError"
except install.InstallError:
    ok("install: 坏 skill 拒绝入内")
try:
    install.install_skill("no-such-name", st)  # 既非市场名,也不是存在的目录
    assert False, "expected InstallError"
except install.InstallError:
    ok("install: 未知来源抛错")

# ---- 3. 文档审批门:决策层(直接调 check) ----
ANSWERS = []


def fake_input(prompt=""):
    return next(ANSWERS)


builtins.input = fake_input

store = PhaseStore(tmp)
store.reset()
store.transition(Stage.PRD_DRAFT)
gate._PHASE = store  # 直接注入真相(等价 install 干的事,但不碰 permission/_GRANTS)

ANSWERS = iter(["n", "because wrong direction"])
v = gate.check("write", {"file_path": "PRD.md", "content": "v1"})
assert v is not None and "user rejected PRD.md" in v and "Revise" in v; ok("gate: 否决串 + 返工措辞")
assert store.current == Stage.PRD_DRAFT; ok("gate: n 不推进阶段")

ANSWERS = iter(["y"])
assert gate.check("write", {"file_path": "PRD.md", "content": "v2"}) is None; ok("gate: y 放行")
assert store.current == Stage.PRD_APPROVED; ok("gate: y 推进阶段")

# draft 阶段写非产物 / 非 write 工具,都不拦(且不该提问)
ANSWERS = iter([])
assert gate.check("write", {"file_path": "README.md", "content": "x"}) is None; ok("gate: 非产物不拦")
assert gate.check("read", {"file_path": "PRD.md"}) is None; ok("gate: 非 write 不拦")

# ---- 4. loop 集成:模型写 PRD.md → 两道门 → on_stop 自动推进 SPEC ----
hooks.PRE_TOOL_USE[:] = [gate.check]
hooks.STOP[:] = [gate.on_stop]
context.reset()


class FakeModel(Model):
    def __init__(self, script):
        self.script = list(script)

    def complete(self, messages, system, tools, max_tokens=8000):
        return self.script.pop(0)


def w(i, path, content):
    return {"content": [{"type": "tool_use", "id": i, "name": "write",
                         "input": {"file_path": path, "content": content}}],
            "stop_reason": "tool_use", "usage": {"input": 10, "output": 5}}


def e(text):
    return {"content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "usage": {"input": 20, "output": 5}}


store2 = PhaseStore(tmp)
store2.reset()
store2.transition(Stage.PRD_DRAFT)
gate._PHASE = store2

script = FakeModel([
    w("t1", "PRD.md", "draft one"),      # 人 n → 拒绝,不落盘
    w("t2", "PRD.md", "draft two"),      # 人 y → 落盘 + 阶段推进
    e("PRD done"),                       # on_stop → SPEC_DRAFT → continue
    e("waiting for next instruction"),   # SPEC_DRAFT → 放行收工
])
ANSWERS = iter(["n", "too vague", "y"])  # 否决要喂两个:结论 + 理由
context.append_user("write prd")
answer, _ = loop.run(script, "sys")
assert "waiting for next instruction" in answer; ok("gate/loop: 跑完脚本")
assert (tmp / "PRD.md").read_text(encoding="utf-8") == "draft two"; ok("gate/loop: y 才真正落盘")
assert store2.current == Stage.SPEC_DRAFT; ok("gate/loop: on_stop 自动推进 spec_draft")

trs = [b for m in context.emit() if isinstance(m.get("content"), list)
       for b in m["content"] if b.get("type") == "tool_result"]
assert any(t.get("is_error") and "user rejected PRD.md" in t["content"] for t in trs)
ok("gate/loop: n → 否决以 is_error 带回给模型")

print(f"\nALL {PASS} assertions passed")