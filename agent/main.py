from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from agent import context, cron, goal, hooks, loop, mcp, skills, subagent, team, tools, workflow
from agent.config import load_config
from agent.memory import agentsmd, facts, sessions
from agent.models import get_model


def _show_sessions(sess_list: list) -> None:
    for i, s in enumerate(sess_list, 1):
        m = sessions.read_meta(s)
        state = "closed" if m.get("closed") else "open  "
        title = m.get("title") or "(无标题)"
        n = sessions.count_messages(s)
        tok = m.get("input", 0) + m.get("output", 0)
        print(f"  {i}. [{state}] {s.id}  {n:>3}条 {tok:>7,}tok  {title}")


def _ask_index(n: int) -> int | None:
    """选单:1..n 挑,回车/非法 → 最新(None 以外),0 → 新会话(None)。

    n >= 1 由调用方保证。越界数字夹到 [1,n](1 起算,'99' 落到最老);
    非数字/空/负数一律当"没想好" → 最新。EOF/Ctrl-C 同样不炸。
    """
    try:
        raw = input(f"  选一个(1-{n},回车=最新,0=新会话): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    if not raw:
        return 0
    try:
        i = int(raw)
    except ValueError:
        return 0
    if i == 0:
        return None  # 明确要新会话,不是数字越界
    return max(1, min(i, n)) - 1


def _open_session(args: list[str], sessions_root: Path) -> list[dict]:
    """选会话,返回要回放的历史。默认续最近;--new 开新;--resume 列表挑。

    三种情况都只 attach 或不 attach,CURRENT 仍是 None 时由调用方 begin()。
    """
    existing = sessions.list_sessions(sessions_root)

    if "--resume" in args:
        if not existing:
            print("  [memory] 没有可恢复的会话,开始新会话")
            return []
        _show_sessions(existing)
        idx = _ask_index(len(existing))
        if idx is None:
            print("  [memory] 不挑了,开新会话")
            return []
        s = existing[idx]
        sessions.attach(s)
        print(f"  [memory] 恢复会话 {s.id}: {sessions.read_meta(s).get('title', '(无标题)')}")
        return sessions.read_messages(s)

    if "--new" in args:
        print("  [memory] --new:另起一个新会话")
        return []

    if not existing:
        print("  [memory] 没有历史会话,开始新会话")
        return []

    s = existing[0]
    sessions.attach(s)
    m = sessions.read_meta(s)
    dirty = "" if m.get("closed") else "  ⚠ 上次未正常退出"
    print(f"  [memory] 续最近会话 {s.id}: {m.get('title', '(无标题)')}{dirty}")
    return sessions.read_messages(s)


def _transcript(msgs: list[dict]) -> str:
    """会话原文 → 平文本,给 consolidation 当原料。"""
    lines: list[str] = []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, str):
            lines.append(f"{m.get('role')}: {c}")
        else:
            lines.append(f"{m.get('role')}: " + json.dumps(c, ensure_ascii=False))
    return "\n".join(lines)


def _consolidate(model_obj) -> None:
    """退出时沉淀事实:太短的会话不沉淀;LLM 失败静默 —— 记忆绝不拦退出。"""
    s = sessions.CURRENT
    if s is None:
        return
    msgs = sessions.read_messages(s)
    if len(msgs) < 4:
        return
    notes = facts.consolidate(model_obj, _transcript(msgs))
    if not notes:
        return
    facts.append_notes(Path.cwd() / ".idea2app" / "memory", notes)
    print("  [memory] 已把本次会话要点沉淀进 .idea2app/memory/project.md")


def main() -> None:
    config = load_config()
    model_obj = get_model(config)
    subagent.set_model(model_obj, config.max_tokens)
    team.set_model(model_obj, config.max_tokens)
    workflow.set_runner(model_obj)

    eval_model_id = os.getenv("GOAL_EVALUATOR_MODEL_ID")
    if eval_model_id:
        from dataclasses import replace

        goal.bind(get_model(replace(config, model=eval_model_id)))
    else:
        goal.bind(model_obj)

    user_dir = Path.home() / ".idea2app"
    project_dir = Path.cwd() / ".idea2app"

    skills.load_skills(project_dir / "skills", user_dir / "skills")

    # ---- 会话:默认续最近(--new 开新, --resume 列表挑) ----
    sessions_root = project_dir / "sessions"
    history = _open_session(sys.argv[1:], sessions_root)
    if sessions.CURRENT is None:
        sessions.begin(sessions_root)

    # ---- system = 身份 + 约定(AGENTS.md) + 事实记忆 + 技能 ----
    system = (
        f"You are Idea2App, a coding agent working in {os.getcwd()}. "
        "Use the bash tool to solve tasks. Act, don't explain. "
        "For multi-step work with dependencies, track it in the task system: "
        "create every task node first with create_task, then wire dependencies "
        "with update_task using the ids create_task returned. Claim a task only "
        "when its dependencies are done, and complete it once finished. "
        "For a simple in-session checklist, use todo_write instead. "
        "Set run_in_background=true on a bash call only for slow, independent commands. "
        "For time-based work, use schedule_cron (list with list_crons, stop with cancel_cron). "
        "For external tools, call connect_mcp first, then the mcp__<server>__<tool> it discovers. "
        "For parallel work, spawn_teammate to farm tasks out to a team (list_teammates, "
        "send_message, request_plan/review_plan to gate a plan, request_shutdown to stop one); "
        "teammate results arrive as [Team events] between turns. "
        "For orchestration of many sub-steps, use Workflow."
    )
    agents = agentsmd.load(user_dir, project_dir)
    if agents:
        system += "\n\n" + agents
    facts_block = facts.inject(user_dir / "memory", project_dir / "memory")
    if facts_block:
        system += "\n\n" + facts_block

    # ---- 拼prompt ----
    system += skills.format_for_prompt()
    system = mcp.assemble_system_prompt(system)

    context.setup(system, tools.api_tools(), config)
    context.replay(history)  # 恢复历史(若有)—— setup 已清空窗口,回放必须在它之后

    # ---- s12/s13:REPL 回合 / cron 投递 / team 事件三处共享一把锁 ----
    # 谁在跑模型回合谁持锁;input() 在锁外阻塞,队友/cron 趁空闲把事件投进来。
    agent_lock = threading.Lock()
    stop_wake = threading.Event()

    def _deliver_team_events() -> None:
        msgs = team.consume_lead_inbox()
        if not msgs:
            return
        text = team.format_team_events(msgs)
        print(f"\n{text}\n")
        context.append_injected(text)
        sessions.record({"role": "user", "content": text})

    def _run_scheduled_turn() -> None:
        jobs = cron.CRON.consume_cron_queue()
        if not jobs:
            return
        print(f"  [cron] delivering {len(jobs)} scheduled job(s)")
        try:
            for job in jobs:
                prompt = f"[scheduled task] {job.prompt}"
                context.append_user(prompt)
                sessions.record({"role": "user", "content": prompt})
                answer, _ = loop.run(model_obj, system, config.max_tokens)
                print(f"  [cron] {job.id} -> {answer[:200]}")
            cron.CRON.acknowledge_cron_jobs(jobs)
            _deliver_team_events()
        except Exception as exc:
            cron.CRON.restore_cron_jobs(jobs)
            print(f"  [cron] scheduled turn failed: {type(exc).__name__}: {exc}")

    def _team_wake_loop() -> None:
        while not stop_wake.is_set():
            if team.has_lead_events():
                with agent_lock:
                    _deliver_team_events()
            stop_wake.wait(1.0)

    cron.CRON.start(agent_lock, _run_scheduled_turn)
    threading.Thread(target=_team_wake_loop, daemon=True, name="team-wake").start()

    print(f"=== Idea2App (models) === [{config.provider}] {config.model}"
          + (f" [恢复 {len(history)} 条]" if history else ""))
    print("Type a task, Enter to send. /<skill> to invoke a skill. "
          "/goal <condition> to set a completion goal. q to quit.\n")

    session_usage = {"input": 0, "output": 0}

    while True:
        try:
            query = input("idea2app >> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if query.lower() in ("q", "exit", ""):
            break

        # ---- 一个模型回合 = 一个 agent_lock 临界区。REPL / cron / team wake
        # 三处争这一把锁,保证 context、sessions、goal 眼里只有一个线程 ----
        with agent_lock:
            # 先投递队友这段时间攒下的事件,再处理用户输入
            _deliver_team_events()

            # ---- /goal 会话级目标闸门(s17):status / clear / set ----
            goal_arg = goal.parse_command(query)
            if goal_arg is not None:
                if not goal_arg:
                    print(goal.GOAL.status(session_usage["input"] + session_usage["output"]))
                    continue
                if goal_arg.lower() in goal.CLEAR_ALIASES:
                    print(goal.GOAL.clear())
                    continue
                goal.GOAL.set_goal(goal_arg, session_usage["input"] + session_usage["output"])
                print(f"  [goal] 已设定完成条件: {goal_arg}")
                query = goal_arg  # 完成条件即本轮任务,直接喂给主循环开干

            query = hooks.fire_submit(query)
            query = skills.maybe_expand(query)
            context.append_user(query)
            sessions.record({"role": "user", "content": query})
            answer, task_usage = loop.run(model_obj, system, config.max_tokens)
            session_usage["input"] += task_usage["input"]
            session_usage["output"] += task_usage["output"]
            _deliver_team_events()

        print(f"\n{answer}\n")
        print(
            f"[usage] task: in {task_usage['input']:,}, out {task_usage['output']:,}"
            f" | session: in {session_usage['input']:,}, out {session_usage['output']:,}"
        )
        print(f"[context] {context.pressure_line()}")

    stop_wake.set()
    with agent_lock:
        pass  # 等在途的 wake 投递收尾,别再碰 sessions 了
    cron.CRON.stop()

    saved = sessions.CURRENT.dir if sessions.CURRENT else None
    _consolidate(model_obj)
    sessions.finish(usage=session_usage)
    print(
        f"bye. session total: input {session_usage['input']:,} tokens, "
        f"output {session_usage['output']:,} tokens"
        + (f" | saved to {saved}" if saved else "")
    )


if __name__ == "__main__":
    main()