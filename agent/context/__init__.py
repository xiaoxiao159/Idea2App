"""context 工程的入口:会话状态(窗口/档案)+ 装配 + 观测。

状态住这(模块全局),对齐 skills._SKILLS / todo._TODOS 的老规矩:进程死即随会话亡。
窗口是唯一真相源;送 LLM 前由 emit() 投影成纯 messages —— models/ 得到的仍是纯 dict。
"""
from __future__ import annotations

from typing import Any

from agent.config import Config
from agent.context import archive, meter
from agent.context.base import Budget, Entry

# ---- 会话状态(进程死即亡)----
WINDOW: list[Entry] = []
_BASELINE: int | None = None
_BUDGET: Budget | None = None
_EPOCH = 0


def reset() -> None:
    """清空窗口与档案、抹去基线/预算。测试隔离用;正常会话不调。"""
    global _BASELINE, _BUDGET, _EPOCH
    WINDOW.clear()
    archive.clear()
    _BASELINE = None
    _BUDGET = None
    _EPOCH = 0


def setup(system: str, tools: list[dict], config: Config) -> None:
    """启动一次:先 reset,再算基线、造预算。窗口从空开始。"""
    global _BASELINE, _BUDGET
    reset()
    _BASELINE = meter.baseline_tokens(system, tools)
    _BUDGET = meter.make_budget(config.window, config.max_tokens, meter.RESERVE_RATIO)


# ---- 写入窗口(四类入账,pin 由入账场景显式定,不靠 role 猜)----


def append_user(text: str) -> None:
    """用户原话:pin=True(压不得也删不得)。"""
    WINDOW.append(meter.stamp({"role": "user", "content": text}, pin=True, epoch=_EPOCH))


def append_assistant(content: list[dict]) -> None:
    """模型回复(文本/tool_use 块):可压缩可淘汰。"""
    WINDOW.append(meter.stamp({"role": "assistant", "content": content}, pin=False, epoch=_EPOCH))


def append_results(results: list[dict]) -> None:
    """工具结果回传(挂在 role=user 的 tool_result 块):可压缩可淘汰。"""
    WINDOW.append(meter.stamp({"role": "user", "content": results}, pin=False, epoch=_EPOCH))


def append_feedback(text: str) -> None:
    """护栏注入(否决理由、中断提示):虽挂在 user,但它是人的指令,pin=True。"""
    WINDOW.append(meter.stamp({"role": "user", "content": text}, pin=True, epoch=_EPOCH))


def append_injected(text: str) -> None:
    """后台结果注入:并进上一条 user 消息,避免连续两条 user 触发 API 交替校验。

    上一条是 user 就并(字符串拼接 / 块列表追加 text 块),否则新开一条 user。
    pin=False:它是状态汇报,不是人的指令,压缩轮可淘汰。
    """
    if WINDOW and WINDOW[-1].msg.get("role") == "user":
        last = WINDOW[-1]
        content = last.msg["content"]
        if isinstance(content, list):
            content = content + [{"type": "text", "text": text}]
        else:
            content = f"{content}\n\n{text}" if content else text
        merged = {"role": "user", "content": content}
        WINDOW[-1] = meter.stamp(merged, pin=last.pin, epoch=last.epoch)
    else:
        WINDOW.append(meter.stamp({"role": "user", "content": text}, pin=False, epoch=_EPOCH))


# ---- 读取窗口(投影)----


def emit() -> list[dict]:
    """Entry->msg"""
    return [entry.msg for entry in WINDOW]


def replay(messages: list[dict]) -> None:
    """
    将msg->Entry
    """
    for msg in messages:
        pin = msg.get("role") == "user" and isinstance(msg.get("content"), str)
        WINDOW.append(meter.stamp(msg, pin=pin, epoch=_EPOCH))


# ---- 观测(给人看)----


def pressure_line() -> str:
    """一行液位报告。没 setup 就直说;setup 了才报百分比。"""
    if _BASELINE is None or _BUDGET is None:
        return "context: not set up"
    used = _BASELINE + sum(e.tokens for e in WINDOW)
    p = meter.pressure(WINDOW, _BASELINE, _BUDGET)
    return f"pressure {p:.1%} ({used:,}/{_BUDGET.input_budget:,} tok est) | archive {archive.tokens():,} tok"


def note_archival() -> None:
    """压缩轮次 +1。compact 每归档一回合就盖个戳,新条目落进新的 epoch。"""
    global _EPOCH
    _EPOCH += 1