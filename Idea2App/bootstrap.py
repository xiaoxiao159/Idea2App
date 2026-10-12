"""Idea2App 接线层:把「引擎装配 + 两阶段指令 + 一轮驱动」抽到一处,终端/web 两个壳共用。

终端壳 = main.py 的 REPL;web 壳 = backend/server.py 的 SSE 服务。两者共享本文件的
setup() 与指令,避免两份会漂移的接线。agent 一个字节不动的约束不变:只走 agent 的
对外缝(skills.load_skills / hooks.* / context.setup+loop.run)。

setup() 的关键注入点:
  ask   = (artifact, content) -> None/str,喂给 gate.install 替换内置 terminal input;
  pre_hooks / post_hooks / stop_hooks = 观测回调(web 用来发 tool_call/tool_result 事件),
           pre 在 gate.check 之前追加,保证「工具芯片」先于「审批卡」到达浏览器。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent  # code_agent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from agent import context, goal, skills, subagent, team, workflow  # noqa: E402
from agent.config import Config, load_config  # noqa: E402
from agent.loop import run as run_loop  # noqa: E402
from agent.memory import sessions  # noqa: E402
from agent.models import get_model  # noqa: E402
from agent.tools import api_tools  # noqa: E402

from Idea2App import gate  # noqa: E402
from Idea2App.phase import PhaseStore, Stage  # noqa: E402
from Idea2App.settings import Settings  # noqa: E402

SYSTEM_BASE = (
    f"You are Idea2App, a coding agent working in {os.getcwd()}. "
    "Turn an idea into a working app in two phases.\n"
    "Phase 1 (planning): produce PRD.md (product requirements), then SPEC.md (technical spec). "
    "Each is reviewed by a human before acceptance; if rejected, revise and write again.\n"
    "Phase 2 (self-driven): once SPEC.md is frozen, implement it. For multi-step work with "
    "dependencies, create every task node with create_task, wire dependencies with update_task, "
    "claim a task only when its dependencies are done, complete it when finished. For a simple "
    "in-session checklist use todo_write. Set run_in_background=true on bash only for slow, "
    "independent commands. For parallel work use spawn_teammate; to orchestrate many sub-steps "
    "use Workflow. Use bash to run commands. Act, don't explain.\n"
    "The current phase's instruction arrives with each turn."
)

BUILD_INSTRUCTION = (
    "Phase 2 — SPEC.md is frozen. Implement the app exactly per SPEC.md. "
    "First re-read SPEC.md, plan the work with create_task (or todo_write for a short "
    "checklist), then build it, claiming and completing tasks as you go."
)


def phase_line(store: PhaseStore) -> str:
    return f"[phase: {store.current.value}]"


def deliver_team_events() -> None:
    msgs = team.consume_lead_inbox()
    if not msgs:
        return
    text = team.format_team_events(msgs)
    print(f"\n{text}\n")
    context.append_injected(text)
    sessions.record({"role": "user", "content": text})


def prd_instruction(idea: str) -> str:
    return (
        f"New idea: {idea}\n\n"
        "Phase 1 — write PRD.md (product requirements) at the project root: "
        "product goal, target users, core features, and explicit scope. "
        "It will be reviewed by a human before acceptance."
    )


class App:
    """一轮装配好的引擎 + 阶段真相;run() 驱动一轮(synchronous,阻塞)。"""

    def __init__(self, config, model, store: PhaseStore, settings: Settings, system: str):
        self.config = config
        self.model = model
        self.store = store
        self.settings = settings
        self.system = system

    def run(self, text: str, model=None) -> tuple[str, dict]:
        m = model or self.model
        context.append_user(text)
        sessions.record({"role": "user", "content": text})
        answer, usage = run_loop(m, self.system, self.config.max_tokens)
        deliver_team_events()
        return answer, usage


def setup(settings: Settings | None = None, ask=None,
          pre_hooks=(), post_hooks=(), stop_hooks=(), config: Config | None = None) -> App:
    """装配引擎并返回 App。ask/hooks 见模块 docstring。

    config 非 None 时直接注入(web 多项目模式:worker 用 build_config(os.environ)
    从注入的环境变量造 Config,避免 load_config 里 load_dotenv(override=True) 用
    仓库 .env 覆盖注入值);None 则 load_config() 读 .env —— 兼容终端壳 / 单机模式。
    """
    if config is None:
        config = load_config()
    model = get_model(config)
    subagent.set_model(model, config.max_tokens)
    team.set_model(model, config.max_tokens)
    workflow.set_runner(model)
    goal.bind(model)

    settings = settings or Settings.from_env()
    skills.load_skills(settings.project_skills_dir, settings.skills_dir)

    store = PhaseStore(settings.project_dir)

    from agent import hooks
    for h in pre_hooks:
        if h not in hooks.PRE_TOOL_USE:
            hooks.PRE_TOOL_USE.append(h)
    gate.install(store, ask=ask)
    for h in post_hooks:
        if h not in hooks.POST_TOOL_USE:
            hooks.POST_TOOL_USE.append(h)
    for h in stop_hooks:
        if h not in hooks.STOP:
            hooks.STOP.append(h)

    sessions.begin(settings.project_dir / ".idea2app" / "sessions")

    system = SYSTEM_BASE + skills.format_for_prompt()
    context.setup(system, api_tools(), config)
    return App(config, model, store, settings, system)