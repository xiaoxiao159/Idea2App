"""Skills:把可复用的工作方法打包成目录,模型按需取用。格式照 Claude Code。

一个 skill = 一个目录:SKILL.md(YAML frontmatter + markdown 正文)+ 可选辅助文件。
frontmatter 只认 name/description,其余键忽略(前向兼容,allowed-tools 以后再说)。

渐进式披露是灵魂——上下文预算花在刀刃上:
- L1 启动:仅 name+description 进 system prompt(每个 skill 几十 token)
- L2 调用:SKILL.md 正文此刻才进上下文,只有被用到的 skill 才花
- L3 辅助文件:正文指引模型用 read 按需读(render 带的 path 就是钥匙)

装载:先扫用户级,后扫项目级,同名覆盖(覆盖=扫描顺序,不是魔法)。
非法 skill 警告一行+跳过,绝不炸启动——一个坏文件不配杀掉整个会话。
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# Agent Skills 规范:name 小写字母/数字/连字符 ≤64;description ≤1024。
# 数字来自规范记忆(官方文档本会话不可达),真疼了再改。
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_BODY = 50_000  # SKILL.md 是用户可控文件,不设限则 1MB 正文每次调用全文进 context


@dataclass(frozen=True)
class Skill:
    """name/description 给 L1 用;base_dir/body 给 L2/L3 用。"""

    name: str
    description: str
    base_dir: Path
    body: str


# 会话状态:已装载的 skill 表。启动装一次,进程退出即消失(不热加载)。
_SKILLS: dict[str, Skill] = {}


def _warn(path: Path, reason: str) -> None:
    """坏 skill 只配一行 stderr,不配炸启动。"""
    print(f"  [skills] skip {path}: {reason}", file=sys.stderr)


def _parse_skill_md(path: Path) -> Skill | None:
    """解析+校验一份 SKILL.md。合法 → Skill;非法 → 已警告,返回 None。

    读不进来/解析不了/校验不过,统统走同一条出口:警告+跳过。
    """
    try:
        # utf-8-sig:中文 Windows 记事本爱写 BOM,顺带剥掉;无 BOM 时是 no-op
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:  # GBK 编码文件在中文 Windows 真会发生
        _warn(path, f"unreadable: {exc}")
        return None

    lines = text.splitlines()  # universal newlines 已把 \r\n 归一成 \n
    if not lines or lines[0] != "---":
        _warn(path, "must start with '---' frontmatter")
        return None
    closer = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
    if closer is None:
        _warn(path, "frontmatter not closed with '---'")
        return None

    try:
        meta = yaml.safe_load("\n".join(lines[1:closer]))
    except yaml.YAMLError as exc:  # 手写 yaml 第二常见错:tab、未闭合的 [
        _warn(path, f"yaml error: {exc}")
        return None
    if not isinstance(meta, dict):
        _warn(path, "frontmatter is not a mapping")
        return None

    name, desc = meta.get("name"), meta.get("description")
    # isinstance 是给 yaml 自动类型准备的:description: 42 → int,name: true → bool
    if not (isinstance(name, str) and NAME_RE.fullmatch(name) and len(name) <= MAX_NAME):
        _warn(path, f"bad name {name!r}: need ^[a-z0-9][a-z0-9-]*$, <= {MAX_NAME}")
        return None
    if name != path.parent.name:
        _warn(path, f"name {name!r} != directory {path.parent.name!r}")
        return None
    if not (isinstance(desc, str) and desc.strip() and len(desc) <= MAX_DESCRIPTION):
        _warn(path, f"bad description: need non-empty str <= {MAX_DESCRIPTION}")
        return None
    body = "\n".join(lines[closer + 1 :]).strip("\n")
    if not body.strip():
        _warn(path, "empty body")
        return None
    if len(body) > MAX_BODY:
        _warn(path, f"body too long (> {MAX_BODY} chars)")
        return None
    # 构造时拍平 description:yaml | 块标量会写出多行,不拍平会把清单撕成多行
    return Skill(name, " ".join(desc.split()), path.parent, body)


def load_skills(project_dir: Path, user_dir: Path) -> dict[str, Skill]:
    """扫描两级目录,原子置换 _SKILLS,返回它(方便断言)。

    两个必填参数、无默认值:默认路径的计算留在 main 调用点,
    测试物理上碰不到真实 ~/.idea2app(那是 Idea2App 自己的用户级配置)。
    """
    loaded: dict[str, Skill] = {}
    for root in (user_dir, project_dir):  # 先 user 后 project:后者覆盖同名
        if not root.is_dir():
            continue  # 目录不存在 = 这个级别没有 skill,不是错误
        for skill_md in sorted(root.glob("*/SKILL.md")):  # 只扫一层,排序保确定性
            skill = _parse_skill_md(skill_md)
            if skill is not None:
                loaded[skill.name] = skill
    _SKILLS.clear()  # 整表置换,绝不 merge:测试之间才不互相残留
    _SKILLS.update(loaded)
    return _SKILLS


def get_skill(name: str) -> Skill | None:
    return _SKILLS.get(name)


def format_for_prompt() -> str:
    """L1:空表 → ""(拼进 system 无害,不需要 if);非空 → skill 清单一段。"""
    if not _SKILLS:
        return ""
    lines = [
        "",
        "## Available skills",
        "Before starting a task, check this list. If a skill matches,",
        "call the skill tool with its name to load the full instructions,",
        "then follow them. Supporting files live in the skill's directory",
        "and can be read with the read tool.",
    ]
    lines += [f"- {name}: {_SKILLS[name].description}" for name in sorted(_SKILLS)]
    return "\n".join(lines)


def render_invocation(skill: Skill, args: str = "") -> str:
    """
    正文进入上下文
    <skill name="..." path=".../skills/xxx">
        ...SKILL.md 正文...
    </skill>
    （加上 args：Arguments from the caller: ...）
    """
    text = (
        f'<skill name="{skill.name}" path="{skill.base_dir.as_posix()}">\n'
        f"{skill.body}\n</skill>"
    )
    if args:
        text += f"\n\nArguments from the caller: {args}"
    return text


def maybe_expand(query: str) -> str:
    """/name args → 展开成包裹文本;其余原样返回。main 在 fire_submit 之后调用。"""
    if not query.startswith("/"):
        return query
    name, _, args = query[1:].partition(" ")  # partition 而非 split:args 原样保留
    skill = _SKILLS.get(name)
    if skill is None:
        print(f"  [skills] no such skill: /{name}", file=sys.stderr)
        return query
    return render_invocation(skill, args)
