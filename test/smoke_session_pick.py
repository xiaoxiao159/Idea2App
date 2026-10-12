"""会话挑选冒烟:_open_session 的默认续 / --new / --resume / 脏会话标记。"""
import builtins
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import main as agentmain  # noqa: E402
from agent.memory import sessions  # noqa: E402

TMP = Path(tempfile.mkdtemp())  # 会话目录进临时目录,不污染项目
os.chdir(TMP)
ROOT = TMP / "sessions"

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


def fresh(sid, title, closed, msgs=()):
    """造一个会话目录:meta + events,id 给定以便控制'谁最新'。"""
    d = ROOT / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        '{"id": "%s", "title": "%s", "closed": %s}'
        % (sid, title, "true" if closed else "false"),
        encoding="utf-8")
    (d / "events.jsonl").write_text(
        "".join('{"role": "%s", "content": "%s"}\n' % m for m in msgs),
        encoding="utf-8")


def capture(fn, *a):
    """跑一段并抓 stdout —— 这些函数靠 print 汇报,断言要看得见它说了什么。"""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        out = fn(*a)
    return out, buf.getvalue()


# ---- 1. 空目录:没有历史 → 不动 CURRENT,交给调用方 begin ----
sessions.CURRENT = None
history, text = capture(agentmain._open_session, [], ROOT)
assert history == [] and sessions.CURRENT is None
assert "没有历史会话" in text
ok("空目录:不提历史、不 attach")

# ---- 2. 默认:续最近(id 倒序的第一个),历史原样回放 ----
fresh("20260101-000001", "老会话", True, [("user", "第一句"), ("assistant", "第一答")])
fresh("20260102-000002", "新会话", True, [("user", "第二句"), ("assistant", "第二答")])
sessions.CURRENT = None
history, text = capture(agentmain._open_session, [], ROOT)
assert sessions.CURRENT is not None and sessions.CURRENT.id == "20260102-000002"
assert [m["content"] for m in history] == ["第二句", "第二答"]
assert "续最近会话 20260102-000002" in text and "新会话" in text
ok("默认:续最近的会话(id 倒序取第一个),历史逐行回放")

# ---- 3. closed=false 是'上次没正常退出'的脏标记,默认续时要点出来 ----
sessions.CURRENT = None
fresh("20260103-000003", "被杀过的", False, [("user", "又一句")])
history, text = capture(agentmain._open_session, [], ROOT)
assert sessions.CURRENT.id == "20260103-000003" and "未正常退出" in text
ok("脏会话(closed=false)被标出来,仍然可续")

# ---- 4. --new:有历史也不续,CURRENT 保持 None ----
sessions.CURRENT = None
history, text = capture(agentmain._open_session, ["--new"], ROOT)
assert history == [] and sessions.CURRENT is None
assert "--new" in text
ok("--new:无视历史,开新会话")

# ---- 5. --continue 仍然认(语义已并入默认,不该报错也不该开新) ----
sessions.CURRENT = None
history, text = capture(agentmain._open_session, ["--continue"], ROOT)
assert sessions.CURRENT is not None and sessions.CURRENT.id == "20260103-000003"
assert len(history) == 1
ok("--continue:向后兼容,等价于默认")

# ---- 6. --resume 选 1 = 最新 ----
sessions.CURRENT = None
_real_input = builtins.input
builtins.input = lambda prompt="": "1"
try:
    history, text = capture(agentmain._open_session, ["--resume"], ROOT)
finally:
    builtins.input = _real_input
assert sessions.CURRENT.id == "20260103-000003"
assert "20260103-000003" in text and "20260102-000002" in text  # 选单里有全部
ok("--resume 1:选单列出全部,选最新")

# ---- 7. --resume 选 3 = 最老;编号 1 起算,别错位 ----
sessions.CURRENT = None
builtins.input = lambda prompt="": "3"
try:
    history, text = capture(agentmain._open_session, ["--resume"], ROOT)
finally:
    builtins.input = _real_input
assert sessions.CURRENT.id == "20260101-000001", sessions.CURRENT.id
assert [m["content"] for m in history] == ["第一句", "第一答"]
ok("--resume 3:选到最老的一个,编号没错位")

# ---- 8. --resume 非法输入:就近夹取,不炸 ----
# 非数字/空/负数 → 最新;越界正数夹到 n(最老)
for raw, want in (("", "20260103-000003"), ("abc", "20260103-000003"),
                  ("-5", "20260103-000003"), ("99", "20260101-000001")):
    sessions.CURRENT = None
    builtins.input = lambda prompt="", _r=raw: _r
    try:
        history, text = capture(agentmain._open_session, ["--resume"], ROOT)
    finally:
        builtins.input = _real_input
    assert sessions.CURRENT.id == want, (raw, sessions.CURRENT.id)
ok("--resume 非法输入(空/字母/负数/越界)都夹到有效范围")

# ---- 8b. 输入 0 = 我不挑了,开新会话:CURRENT 保持 None ----
sessions.CURRENT = None
builtins.input = lambda prompt="": "0"
try:
    history, text = capture(agentmain._open_session, ["--resume"], ROOT)
finally:
    builtins.input = _real_input
assert history == [] and sessions.CURRENT is None
assert "开新会话" in text
ok("--resume 输入 0:取消挑选,开新会话(不 attach 任何旧会话)")

# ---- 8c. 选单要看得见'这会话多大':条数与 token 都在 ----
# 提示语是 input() 的 prompt,绕过 sys.stdout 重定向,得直接抓 prompt 参数
seen_prompts = []


def _recording_input(prompt=""):
    seen_prompts.append(prompt)
    return "0"


sessions.CURRENT = None
builtins.input = _recording_input
try:
    _h, text = capture(agentmain._open_session, ["--resume"], ROOT)
finally:
    builtins.input = _real_input
assert "20260103-000003" in text and "20260101-000001" in text
assert "1条" in text                                  # 每个会话的往返条数
assert any("0=新会话" in p for p in seen_prompts), seen_prompts  # 出口写在提示上
ok("选单:列全所有会话,带条数/用量,并把 0=新会话 写在提示上")


def _eof(prompt=""):
    raise EOFError


sessions.CURRENT = None
builtins.input = _eof
try:
    _open_out, text = capture(agentmain._open_session, ["--resume"], ROOT)
finally:
    builtins.input = _real_input
assert sessions.CURRENT.id == "20260103-000003"
ok("--resume 撞 EOF:退回最新,不抛出来")

# ---- 9. --resume 但无历史:不选单,直接新会话 ----
sessions.CURRENT = None
empty_root = TMP / "empty"
history, text = capture(agentmain._open_session, ["--resume"], empty_root)
assert history == [] and sessions.CURRENT is None and "没有可恢复" in text
ok("--resume 无历史:不摆空选单")

# ---- 10. 坏行不炸恢复:events.jsonl 尾巴半行被跳过 ----
sessions.CURRENT = None
d = ROOT / "20260104-000004"
d.mkdir(parents=True, exist_ok=True)
(d / "meta.json").write_text('{"id": "20260104-000004", "title": "半行", "closed": true}', encoding="utf-8")
(d / "events.jsonl").write_text(
    '{"role": "user", "content": "完整的一行"}\n{"role": "assistant", "conte',
    encoding="utf-8")
history, text = capture(agentmain._open_session, [], ROOT)
assert [m["content"] for m in history] == ["完整的一行"]
ok("坏行:退出瞬间写坏的半行被跳过,不炸掉整个恢复")

print(f"\nALL {PASS} assertions passed")
