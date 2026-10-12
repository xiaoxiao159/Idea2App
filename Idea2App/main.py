"""Idea2App 宿主(终端壳):在 agent 引擎(agent/)之上封装两阶段流程。agent 一个字节不改。

命令:
  /new-app <一句话想法>    进入 Phase 1:先产 PRD.md(人审),过了再产 SPEC.md(人审)
  /build                   Spec 冻结后进入 Phase 2:自驱开发
  /phase                  看当前阶段
  /install-skill <名|路径|URL>  装 skill(市场清单 or 本地目录/git)
  /reset                  回 IDEA(弃掉本项目流程状态与权限授权)
  /help                   帮助;q/exit 退出
  (在 IDEA 阶段直接敲一句话想法,等价 /new-app)

web 壳在 backend/server.py,两者共用 Idea2App/bootstrap.py 的 setup() 与指令。
"""
from __future__ import annotations

from agent import context, skills  # noqa: E402  (context.pressure_line 用于 _report)
from agent.memory import sessions  # noqa: E402

from Idea2App import bootstrap, install
from Idea2App.phase import Stage


def _print_help() -> None:
    print(
        "commands:\n"
        "  /new-app <idea>        start Phase 1 (PRD -> SPEC, both human-reviewed)\n"
        "  /build                 start Phase 2 (self-driven, requires frozen SPEC)\n"
        "  /phase                 show current phase\n"
        "  /install-skill <src>   install a skill (market name | local dir | git url)\n"
        "  /reset                 return to idea\n"
        "  /help                  this help\n"
        "  q / exit               quit"
    )


def _report(answer: str, usage: dict, store) -> None:
    print(f"\n{answer}\n")
    print(
        f"[usage] in {usage['input']:,} out {usage['output']:,} | "
        f"{bootstrap.phase_line(store)} | {context.pressure_line()}"
    )
    if store.current == Stage.SPEC_APPROVED:
        print("★ SPEC 冻结,Phase 1 完 ★ —— 敲 /build 进入自驱开发(Phase 2)")


def main() -> None:
    app = bootstrap.setup()
    store = app.store
    settings = app.settings
    config = app.config
    turn = app.run

    print(f"=== Idea2App (flow) === [{config.provider}] {config.model}")
    print(f"{bootstrap.phase_line(store)}  skills: {settings.skills_dir}")
    print("Type an idea (or /new-app <idea>) to start, /help for commands, q to quit.\n")

    while True:
        try:
            query = input("idea2app >> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if query.lower() in ("q", "exit", ""):
            break

        # ---- 斜杠命令 ----
        if query.startswith("/"):
            q = query.strip()
            if q == "/help":
                _print_help()
                continue
            if q == "/phase":
                print(f"  {bootstrap.phase_line(store)}")
                continue
            if q == "/reset":
                store.reset()
                print("  [phase] reset -> idea")
                continue
            if q.startswith("/new-app"):
                idea = q[len("/new-app"):].strip()
                if not idea:
                    print("  usage: /new-app <one-line idea>")
                    continue
                if store.current != Stage.IDEA:
                    print("  [phase] 已在流程中,先 /reset 再开新想法")
                    continue
                store.transition(Stage.PRD_DRAFT)
                answer, usage = turn(bootstrap.prd_instruction(idea))
                _report(answer, usage, store)
                continue
            if q == "/build":
                if store.current == Stage.SPEC_APPROVED:
                    store.transition(Stage.EXECUTING)
                    answer, usage = turn(bootstrap.BUILD_INSTRUCTION)
                    _report(answer, usage, store)
                elif store.current == Stage.EXECUTING:
                    print("  [phase] already executing")
                else:
                    print("  [phase] 还没有冻结的 Spec(/new-app 走完 Phase 1 再说)")
                continue
            if q.startswith("/install-skill"):
                src = q[len("/install-skill"):].strip()
                if not src:
                    print("  usage: /install-skill <market name | local dir | git url>")
                    continue
                try:
                    name = install.install_skill(src, settings)
                except install.InstallError as exc:
                    print(f"  [install] {exc}")
                    continue
                skills.load_skills(settings.project_skills_dir, settings.skills_dir)
                print(f"  [install] installed skill '{name}' -> {settings.skills_dir / name}")
                continue
            print(f"  unknown command: {q.split()[0]} (try /help)")
            continue

        # ---- 普通输入:IDEA 阶段视作「新想法」 ----
        if store.current == Stage.IDEA:
            store.transition(Stage.PRD_DRAFT)
            answer, usage = turn(bootstrap.prd_instruction(query))
        else:
            answer, usage = turn(query)
        _report(answer, usage, store)

    sessions.finish()
    print("bye.")


if __name__ == "__main__":
    main()