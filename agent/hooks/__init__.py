from __future__ import annotations

from typing import Any

from agent.hooks import audit, permission
from agent.hooks.base import PostToolUseHook, PreToolUseHook, PromptHook, StopHook


USER_PROMPT_SUBMIT: list[PromptHook] = [audit.on_prompt]
# 计划审批门(plan.check)已退役:todo_write 归入私有记忆,不再事前问人;
# 「越界才问」由 permission.check 独家承担。见 agent/hooks/plan.py(已摘除)。
PRE_TOOL_USE: list[PreToolUseHook] = [permission.check]
POST_TOOL_USE: list[PostToolUseHook] = [audit.on_tool]
STOP: list[StopHook] = [audit.on_stop]


def fire_submit(query: str) -> str:
    """UserPromptSubmit:钩子链式改写,前一个的输出是后一个的输入;None=沿用当前值。"""
    for hook in USER_PROMPT_SUBMIT:
        try:
            rewritten = hook(query)
        except Exception as exc:  
            print(f"  [warn] {hook.__name__} (UserPromptSubmit) failed: {exc}")
            continue
        if rewritten:  
            query = rewritten
    return query


def fire_pre(name: str, params: dict[str, Any]) -> str | None:
    """PreToolUse:依序问,首个非 None 即终审。故障关闭——闸门崩了当否决,不能默认放行。"""
    for hook in PRE_TOOL_USE:
        try:
            veto = hook(name, params)
        except Exception as exc:
            return f"hook {hook.__name__} crashed: {type(exc).__name__}: {exc}"
        if veto:
            return veto
    return None


def fire_post(name: str, params: dict[str, Any], result: str, is_error: bool) -> None:
    """PostToolUse:只观察。崩溃=旁路,观察者不许弄伤被观察的主流程。"""
    for hook in POST_TOOL_USE:
        try:
            hook(name, params, result, is_error)
        except Exception as exc:
            print(f"  [warn] {hook.__name__} (PostToolUse) failed: {exc}")


def fire_stop(answer: str, usage: dict[str, int]) -> str | None:
    """Stop:观察 + 否决收工。崩溃=旁路;驳回理由由 loop 注入为 user 消息后 continue,保险丝封顶。"""
    for hook in STOP:
        try:
            veto = hook(answer, usage)
        except Exception as exc:
            print(f"  [warn] {hook.__name__} (Stop) failed: {exc}")
            continue
        if veto:
            return veto
    return None
