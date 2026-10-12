from __future__ import annotations

from typing import Any

from agent import background, context, goal, hooks, tools
from agent.context import compact
from agent.memory import sessions
from agent.models.base import Model
from agent.tasks import store

MAX_ITERATIONS = 20

# 估算永远只是估算。API 真嫌长时,靠这几个关键词认出 prompt_too_long。
_TOO_LONG_HINTS = (
    "prompt_too_long", "prompt is too long", "too many tokens",
    "maximum context length", "context length", "context window",
)


def _too_long(error: Exception) -> bool:
    msg = str(error).lower()
    return any(hint in msg for hint in _TOO_LONG_HINTS)


# 提取 text,返回用户
def _last_text(content: list[dict]) -> str:
    """拼接 content 里所有 text 块。text/tool_use 混排很常见,逐块过滤。"""
    return "\n".join(b["text"] for b in content if b["type"] == "text")


def _start_background(block: dict) -> tuple[str, bool]:
    """bash + run_in_background=true:命令丢后台线程,先回占位结果。"""
    command = str(block["input"].get("command", ""))
    try:
        bg_id = background.start_background_task(command)
    except Exception as exc:
        return (f"Error: {type(exc).__name__}: {exc}", True)
    return (f"[background task {bg_id} started] result will be collected on a later turn", False)


def _complete(model_obj: Model, system: str, tools_list: list[dict], max_tokens: int) -> dict:
    """跑一次补全。若 API 嫌长,先 force 压一轮、原样重试一次(只救一次)。"""
    try:
        return model_obj.complete(context.emit(), system, tools_list, max_tokens)
    except Exception as error:
        if not _too_long(error):
            raise
        print("  [compact] prompt too long, rescuing once")
        compact.force(model_obj)
        return model_obj.complete(context.emit(), system, tools_list, max_tokens)


def run(model_obj: Model, system: str, max_tokens: int = 8000) -> tuple[str, dict]:
    """
    跑一个任务,直到 end_turn / 异常终态 / 保险丝烧断。

    消息不在这里持有:入账进 context(append_*),发送前 context.emit() 投影。
    所以这里没有 messages 参数——窗口是 context 的唯一真相源。
    """
    task_usage = {"input": 0, "output": 0}
    goal.GOAL.begin_query()  # 每个 user query 开头清连续 block 计数(s17)

    for _iteration in range(MAX_ITERATIONS):
        # 每次开口前先看窗口是否快爆;爆了先分级压一压,再放心送出去
        compact.compact(model_obj)
        for note in background.collect_background_results():
            context.append_injected(note)
        resp = _complete(model_obj, system, tools.api_tools(), max_tokens)
        task_usage["input"] += resp["usage"]["input"]
        task_usage["output"] += resp["usage"]["output"]

        if not resp["content"]:
            resp["content"] = [{"type": "text", "text": "(empty response)"}]
        context.append_assistant(resp["content"])
        sessions.record({"role": "assistant", "content": resp["content"]})

        stop = resp["stop_reason"]

        if stop == "tool_use":
            results: list[dict[str, Any]] = []
            compact_requested = False
            for block in resp["content"]:
                if block["type"] != "tool_use":
                    continue
                if block["name"] == "compact":
                    compact_requested = True

                print(f"  [tool] {tools.summary(block['name'], block['input'])}")
                veto = hooks.fire_pre(block["name"], block["input"])
                if veto:
                    text, is_error = f"Error: {veto}", True
                elif background.should_run_background(block["name"], block["input"]):
                    text, is_error = _start_background(block)
                else:
                    text, is_error = tools.run_tool(block["name"], block["input"])
                hooks.fire_post(block["name"], block["input"], text, is_error)
                preview = text[:200] + ("..." if len(text) > 200 else "")
                print(f"  [tool] {'!' if is_error else '>'} {preview}")

                result: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": text,
                }
                if is_error:
                    result["is_error"] = True
                results.append(result)

            context.append_results(results)
            sessions.record({"role": "user", "content": results})
            if compact_requested:
                # 模型主动喊压:这批工具(可能含写文件)已跑完、回合已闭合,现在压
                compact.force(model_obj)
            continue

        if stop == "end_turn":
            answer = _last_text(resp["content"])
            # 回合边界:释放 lead 已完成的认领(cwd 租约 + 复位计划闸门)。
            # 只清「completed」的任务,in_progress 的不动,所以多调无害。
            store.release_completed_assignment("agent")
            veto = hooks.fire_stop(answer, task_usage)
            if veto:
                # Stop 驳回:理由作为 user 消息注入,循环继续;保险丝封顶
                print(f"  [stop] vetoed: {veto}")
                context.append_feedback(veto)
                sessions.record({"role": "user", "content": veto})
                continue

            # ---- s17:Goal Stop hook。end_turn 只说明「这一轮想停」,
            # 不代表总目标达成——独立的 evaluator 拿着完成条件裁决放不放行 ----
            decision = goal.GOAL.evaluate_after_turn(
                context.emit(), background_running=background.any_running()
            )
            if decision.action == "block":
                # 未达标 → 驳回理由灌回窗口,同一 while 循环 continue 再开一轮
                feedback = goal.GOAL.block_feedback(decision)
                print(f"  [goal] {decision.reason}")
                context.append_feedback(feedback)
                sessions.record({"role": "user", "content": feedback})
                continue
            if decision.action != "allow":
                # 终态(achieved/failed/error/limit/defer):交还控制权,goal 状态如实带出
                print(f"  [goal] {decision.action}: {decision.reason}")
                return answer + f"\n[goal {decision.action}: {decision.reason}]", task_usage
            return answer, task_usage

        # ---- 异常终态:max_tokens / stop_sequence / 未知 ----
        print(f"  [warn] abnormal stop_reason: {stop!r}")

        pending = [b for b in resp["content"] if b["type"] == "tool_use"]
        if pending:
            interrupted = [
                {
                    "type": "tool_result",
                    "tool_use_id": b["id"],
                    "content": f"Error: interrupted (stop_reason={stop}), tool not executed",
                    "is_error": True,
                }
                for b in pending
            ]
            context.append_results(interrupted)
            sessions.record({"role": "user", "content": interrupted})
        store.release_completed_assignment("agent")
        text = _last_text(resp["content"])
        return text + f"\n[interrupted: stop_reason={stop}]", task_usage

    print(f"  [fuse] MAX_ITERATIONS={MAX_ITERATIONS} reached, force stop")
    store.release_completed_assignment("agent")
    answer = "(no assistant message)"
    for msg in reversed(context.emit()):
        if msg["role"] == "assistant":
            answer = _last_text(msg["content"])
            break
    return answer + f"\n[fuse: MAX_ITERATIONS={MAX_ITERATIONS} reached]", task_usage