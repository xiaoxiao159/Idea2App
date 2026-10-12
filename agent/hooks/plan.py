from __future__ import annotations

from typing import Any

from agent.tools import todo

# 人最近一次点头的 content 集合;None = 本会话还没批过任何计划。
_APPROVED: set[str] | None = None


def _ask(todos: list[dict]) -> str | None:
    """渲染计划,REPL 提问。y → None(放行);n → 追问理由(可空)→ 否决串。
    措辞方向与 permission 相反:权限否决封死重试(换路走),计划否决要求返工(改了再来)。"""
    print(todo.render(todos))
    try:
        answer = input("\n  approve this plan? [y]es / [n]o (Enter=n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "user did not approve this plan. Revise the todo list and call todo_write again."
    if answer == "y":
        return None
    try:
        reason = input("  reason (Enter = none): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        reason = ""
    why = f": {reason}" if reason else ""
    return (
        f"user rejected this plan{why}. "
        "Revise the todo list (drop or rework the rejected items) and call todo_write again."
    )


def check(name: str, params: dict[str, Any]) -> str | None:
    """PreToolUse 消费者:默许 None / 否决理由 str。只对 todo_write 发言。"""
    global _APPROVED
    if name != "todo_write":
        return None

    todos = params.get("todos")
    err = todo.validate(todos)
    if err:
        return err  # 非法提案:当否决退回模型,别拿着垃圾清单去问人

    contents = {str(t["content"]) for t in todos}
    if contents == _APPROVED:
        return None  # 工作集合没动:纯进度汇报,免问
    if _APPROVED is None and not contents:
        return None  # 空对空:还没批过任何东西,也没东西可清

    veto = _ask(todos)
    if veto:
        return veto
    _APPROVED = contents  # y:这份工作集合作数,此后 status 随便改
    return None
