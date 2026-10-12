"""subagent:在全新 messages[] 里跑第二段 agent loop,只把最终文本还给父循环。

隔离的是消息,不是进程或文件系统:子 agent 与父 agent 共享 cwd(写文件、跑 bash
都影响同一工作区),共享权限/审计 hooks —— 子 agent 的 bash/write/edit 照样过闸门。
子 agent 只有四件基础工具(bash/read/write/edit),没有 task/todo_write/skill/compact:
不能再逐层委派,也不会污染父级的 todo 清单与窗口状态。

对应 learn-claude-code s06:Subagent - fresh messages, final text returns。
"""
from __future__ import annotations

import os
from typing import Any

from agent import hooks
from agent.models.base import Model
from agent.tools.bash import BASH_TOOL
from agent.tools.files import EDIT_TOOL, READ_TOOL, WRITE_TOOL

MAX_SUB_ITERATIONS = 30

SUB_SYSTEM = (
    f"You are a focused subagent working in {os.getcwd()}. "
    "Complete the given task with the tools you have, then return a concise "
    "final answer summarizing what you found or did. Do not ask for more input."
)

# 子 agent 的工具集:只有"动手"的基础件,没有编排件(task/todo/skill/compact)。
_SUB_TOOLS = [BASH_TOOL, READ_TOOL, WRITE_TOOL, EDIT_TOOL]
_SUB_HANDLERS = {t.schema["name"]: t for t in _SUB_TOOLS}


# ---- 模型绑定:进程级单例 ----
CURRENT_MODEL: Model | None = None
CURRENT_MAX_TOKENS: int = 8000


def set_model(model_obj: Model, max_tokens: int = 8000) -> None:
    """main 启动时设一次(get_model 之后立刻)。task 的 handler 从这里取 model。"""
    global CURRENT_MODEL, CURRENT_MAX_TOKENS
    CURRENT_MODEL = model_obj
    CURRENT_MAX_TOKENS = max_tokens


def _sub_api_tools() -> list[dict]:
    return [t.schema for t in _SUB_TOOLS]


def _run_sub_tool(name: str, params: dict[str, Any]) -> tuple[str, bool]:
    tool = _SUB_HANDLERS.get(name)
    if tool is None:
        return (f"Error: unknown tool '{name}'", True)
    try:
        return tool.handler(params)
    except Exception as exc:
        return (f"Error: {type(exc).__name__}: {exc}", True)


def _text(content: list[dict]) -> str:
    """拼接 content 里所有 text 块;tool_use 块一律跳过。"""
    return "\n".join(b["text"] for b in content if b.get("type") == "text")


def run_subagent(prompt: str, model_obj: Model, max_tokens: int) -> str:
    """跑一段嵌套循环,返回最终文本。消息不落全局 context——这里是隔离的本体。"""
    print("  [subagent] started")
    messages: list[dict] = [{"role": "user", "content": prompt}]

    for _ in range(MAX_SUB_ITERATIONS):
        resp = model_obj.complete(messages, SUB_SYSTEM, _sub_api_tools(), max_tokens)
        if not resp["content"]:
            resp["content"] = [{"type": "text", "text": "(empty response)"}]
        messages.append({"role": "assistant", "content": resp["content"]})

        tool_calls = [b for b in resp["content"] if b.get("type") == "tool_use"]
        if not tool_calls:
            print("  [subagent] done")
            return _text(resp["content"]) or "(no summary)"

        results: list[dict[str, Any]] = []
        for block in tool_calls:
            print(f"    [sub] {block['name']}")
            veto = hooks.fire_pre(block["name"], block["input"])
            if veto:
                text, is_error = f"Error: {veto}", True
            else:
                text, is_error = _run_sub_tool(block["name"], block["input"])
            hooks.fire_post(block["name"], block["input"], text, is_error)

            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": text,
            }
            if is_error:
                result["is_error"] = True
            results.append(result)
        messages.append({"role": "user", "content": results})

    return f"subagent stopped after {MAX_SUB_ITERATIONS} turns without a final answer."


def run_task(params: dict[str, Any], model_obj: Model, max_tokens: int) -> tuple[str, bool]:
    """底层入口:把 task 工具的入参变成一次子循环,返回 (最终文本, is_error)。
    需显式传 model 与 max_tokens——注册进 REGISTRY 的是下头那个无参包壳 handle_task。"""
    prompt = str(params.get("prompt", "")).strip()
    if not prompt:
        return ("Error: task requires a non-empty prompt", True)
    try:
        return (run_subagent(prompt, model_obj, max_tokens), False)
    except Exception as exc:
        return (f"Error: subagent failed: {type(exc).__name__}: {exc}", True)


def handle_task(params: dict[str, Any]) -> tuple[str, bool]:
    """`task` 工具的真 handler:从模块级 CURRENT_MODEL 取模型,再交给 run_task。

    这就是"定义一个 run_subagent 然后跑实例"的落点:handler 调 run_task,
    run_task 拿当前实例调 run_subagent。模型没设就诚实报错,不留半个假人。
    """
    if CURRENT_MODEL is None:
        return ("Error: subagent model not set (call subagent.set_model first)", True)
    return run_task(params, CURRENT_MODEL, CURRENT_MAX_TOKENS)