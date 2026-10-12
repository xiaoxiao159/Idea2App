"""skills 冒烟:装载校验 / 两级优先 / L1 格式化 / L2 包裹 / 斜杠展开 / 工具层 / loop 集成。"""
import builtins
import io
import os
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import context, hooks, loop, skills, tools  # noqa: E402
from agent.hooks import audit, permission, plan  # noqa: E402
from agent.models.base import Model  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


os.chdir(tempfile.mkdtemp())  # audit 日志落在临时目录

# 两级 skill 目录都用临时目录:load_skills 必填参数,物理上碰不到真实 ~/.idea2app
TMP_P = Path(tempfile.mkdtemp())
TMP_U = Path(tempfile.mkdtemp())

ANSWERS = iter([])


def fake_input(prompt=""):
    return next(ANSWERS)  # 没排到的提问 → StopIteration 当场炸,测试不撒谎


builtins.input = fake_input


def write_skill(root: Path, dirname: str, fm: str, body: str, **kw):
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{fm}\n---\n{body}", **kw)
    return d


def tool_results(messages):
    return [b for m in messages if isinstance(m.get("content"), list)
            for b in m["content"] if b.get("type") == "tool_result"]


# ---- 1. 装载与校验:坏 skill 只警告不炸,好 skill 照常 ----
assert skills.load_skills(TMP_P / "nope", TMP_U / "nope2") == {}
ok("load: 目录不存在 → 空 dict,不炸")
assert skills.format_for_prompt() == ""
ok("format: 空表 → 空串(无孤儿标题)")

write_skill(TMP_P, "alpha", "name: alpha\ndescription: does alpha things", "alpha body line")
for dirname, fm in [
    ("bad-name", "name: other\ndescription: x"),          # name ≠ 目录名
    ("upper", "name: Upper\ndescription: x"),             # 大写,正则不过
    ("under_score", "name: under_score\ndescription: x"), # 下划线,正则不过
    ("num-desc", "name: num-desc\ndescription: 42"),      # yaml 自动类型:int
    ("bool-name", "name: true\ndescription: x"),          # yaml 自动类型:bool
    ("empty-desc", "name: empty-desc\ndescription:"),     # yaml null → None
    ("long-desc", "name: long-desc\ndescription: " + "x" * 1025),
    ("empty-body", "name: empty-body\ndescription: x",),
]:
    if dirname == "empty-body":
        write_skill(TMP_P, dirname, fm, "")
    else:
        write_skill(TMP_P, dirname, fm, "some body")
write_skill(TMP_P, "long-name", "name: " + "a" * 65 + "\ndescription: x", "some body")
write_skill(TMP_U, "long-name", "name: " + "a" * 65 + "\ndescription: x", "some body")  # 两级都写,只算一次跳过

# 手写两种结构性坏文件:缺结束 --- / 根本没有 frontmatter
d = TMP_P / "unclosed"; d.mkdir()
(d / "SKILL.md").write_text("---\nname: unclosed\ndescription: x\n")
d = TMP_P / "nofm"; d.mkdir()
(d / "SKILL.md").write_text("# just markdown\nno frontmatter here\n")
d = TMP_P / "yamllerr"; d.mkdir()
(d / "SKILL.md").write_text("---\nname: [unclosed\n---\nbody")
d = TMP_P / "listfm"; d.mkdir()
(d / "SKILL.md").write_text("---\n- a\n- b\n---\nbody")
d = TMP_P / "gbkfile"; d.mkdir()
(d / "SKILL.md").write_bytes("---\nname: gbkfile\ndescription: 中文\n---\n正文".encode("gbk"))

buf = io.StringIO()
with redirect_stderr(buf):
    loaded = skills.load_skills(TMP_P, TMP_U)
assert "alpha" in loaded; ok("load: 合法 skill 装载")
for bad in ("bad-name", "upper", "under_score", "num-desc", "bool-name",
            "empty-desc", "long-desc", "long-name", "empty-body",
            "unclosed", "nofm", "yamllerr", "listfm", "gbkfile"):
    assert bad not in loaded
ok("load: 14 种非法全部跳过(名字/描述/定界/yaml/编码/空正文)")
assert "skip" in buf.getvalue(); ok("load: 跳过时 stderr 有警告行")
assert len(loaded) == 1; ok("load: 坏文件只警告不炸,好文件照常入表")

write_skill(TMP_P, "bomskill", "name: bomskill\ndescription: bom ok", "bom body", encoding="utf-8-sig")
write_skill(TMP_P, "crlf", "name: crlf\ndescription: crlf ok", "crlf body")
(TMP_P / "crlf" / "SKILL.md").write_text(
    (TMP_P / "crlf" / "SKILL.md").read_text().replace("\n", "\r\n"))
loaded = skills.load_skills(TMP_P, TMP_U)
assert "bomskill" in loaded and "crlf" in loaded
ok("load: BOM 头 / CRLF 行尾 → 照常装载")

write_skill(TMP_U, "shared", "name: shared\ndescription: user version", "USER BODY")
write_skill(TMP_P, "shared", "name: shared\ndescription: proj version", "PROJ BODY")
write_skill(TMP_U, "user-only", "name: user-only\ndescription: only in user", "u body")
write_skill(TMP_P, "proj-only", "name: proj-only\ndescription: only in project", "p body")
loaded = skills.load_skills(TMP_P, TMP_U)
assert loaded["shared"].body == "PROJ BODY"; ok("load: 项目级覆盖用户级同名")
assert "user-only" in loaded and "proj-only" in loaded; ok("load: 两级各自独有项都在")

# ---- 2. format_for_prompt:L1 清单 ----
prompt = skills.format_for_prompt()
assert "## Available skills" in prompt and "- alpha: does alpha things" in prompt
assert "skill tool" in prompt
ok("prompt: 标题 + '- name: description' 行 + skill 工具指引")
assert "- shared: proj version" in prompt; ok("prompt: 覆盖后的 description 是项目级版")
write_skill(TMP_P, "multi", "name: multi\ndescription: |\n  first line\n  second line", "m body")
skills.load_skills(TMP_P, TMP_U)
assert "- multi: first line second line" in skills.format_for_prompt()
ok("prompt: 多行 description 拍平成一行")

# ---- 3. render_invocation:L2 包裹 ----
s = skills.get_skill("alpha")
t = skills.render_invocation(s)
assert t.startswith('<skill name="alpha"') and "path=" in t
assert "alpha body line" in t and t.endswith("</skill>")
ok("render: <skill name=...> 包裹 + path 属性 + 正文")
t = skills.render_invocation(s, "some args")
assert "Arguments from the caller: some args" in t
t = skills.render_invocation(s)
assert "Arguments" not in t
ok("render: args 附注;无 args 不留空尾巴")

# ---- 4. maybe_expand:用户侧通路 ----
assert skills.maybe_expand("/alpha hello world") == skills.render_invocation(s, "hello world")
ok("expand: '/name args' 命中 → 包裹文本")
assert skills.maybe_expand("/alpha") == skills.render_invocation(s)
ok("expand: '/name' 无参 → 正常")
assert skills.maybe_expand("/nope x") == "/nope x"
ok("expand: 未知 '/x' → 原样返回")
assert skills.maybe_expand("plain text") == "plain text"
ok("expand: 非 / 开头 → 原样返回")

# ---- 5. 工具层:模型侧通路 ----
assert "skill" in tools.REGISTRY; ok("registry: 'skill' 已注册")
text, err = tools.run_tool("skill", {"name": "alpha", "args": "go"})
assert err is False and '<skill name="alpha"' in text and "go" in text
ok("tool: 命中 → (body, False)")
text, err = tools.run_tool("skill", {"name": "zzz"})
assert err is True and "unknown skill" in text and "alpha" in text
ok("tool: 未命中 → Error + Available 清单")
text, err = tools.run_tool("skill", {})
assert err is True and "missing required parameter" in text
ok("tool: 缺 name → 友好错误")
assert tools.summary("skill", {"name": "alpha"}) == "skill(alpha)"
assert "skill" in tools.summary("skill", {"name": 42})
ok("display: 摘要;垃圾输入不炸")
permission._GRANTS.clear()
ANSWERS = iter([])
assert permission.check("skill", {"name": "alpha"}) is None
ok("permission: skill 免问直接放行(空 ANSWERS 未消耗)")

# ---- 6. loop 集成:模型调 skill → tool_result 回 body ----
hooks.USER_PROMPT_SUBMIT[:] = [audit.on_prompt]
hooks.PRE_TOOL_USE[:] = [permission.check, plan.check]
hooks.POST_TOOL_USE[:] = [audit.on_tool]
hooks.STOP[:] = [audit.on_stop]
permission._GRANTS.clear()
plan._APPROVED = None

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


context.reset()
context.append_user("use the alpha skill")
ANSWERS = iter([])
answer, _ = loop.run(
    FakeModel([
        tool_use("t1", "skill", {"name": "alpha", "args": "do it"}),
        end_turn("followed the skill"),
    ]),
    "sys",
)
tr = tool_results(context.emit())
assert "followed the skill" in answer
assert tr[0].get("is_error") is not True and '<skill name="alpha"' in tr[0]["content"]
assert "do it" in tr[0]["content"]
ok("集成: 模型调 skill → tool_result 含包裹正文,非错误")


context.reset()
context.append_user("use the zzz skill")
ANSWERS = iter([])
answer, _ = loop.run(
    FakeModel([
        tool_use("t1", "skill", {"name": "zzz"}),
        end_turn("no such skill, told the user"),
    ]),
    "sys",
)
tr = tool_results(context.emit())
assert tr[0].get("is_error") is True and "unknown skill" in tr[0]["content"]
ok("集成: 未知 skill → is_error 回给模型")

log = (Path("logs") / "session.log").read_text(encoding="utf-8")
assert "skill(alpha)" in log; ok("集成: audit 日志含 skill 调用行")

# ---- 7. REPL 顺序:钩子看原始输入,展开在链后 ----
seen = []


def spy(query):
    seen.append(query)
    return query


hooks.USER_PROMPT_SUBMIT[:] = [spy]
raw = "/alpha some task"
q = hooks.fire_submit(raw)
q = skills.maybe_expand(q)
assert seen == [raw] and '<skill name="alpha"' in q
ok("顺序: UserPromptSubmit 看到原始 '/命令',展开发生在链后")

# 还原真实装配(照 smoke_hooks 的收尾)
hooks.USER_PROMPT_SUBMIT[:] = [audit.on_prompt]
hooks.PRE_TOOL_USE[:] = [permission.check, plan.check]
hooks.POST_TOOL_USE[:] = [audit.on_tool]
hooks.STOP[:] = [audit.on_stop]

print(f"\nALL {PASS} assertions passed")
