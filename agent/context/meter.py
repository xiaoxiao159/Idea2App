"""
计量:把消息翻译成 token 估算,把预算算成额度。全是纯函数。
"""
from __future__ import annotations

import json
from typing import Any

from agent.context.base import Budget, Entry

# 估算系数 chars/N。N 取小 → 高估 → 方向朝"早压"偏(高估持有、低估容量)。
CHARS_PER_TOKEN = 3

# 安全垫:永远别把窗口用到满,给估算误差和突发长输出留口气。
RESERVE_RATIO = 0.2


def estimate_tokens(text: str) -> int:
    """chars/N 向上取整;空串也占 1——空消息发出去一样花 token。"""
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def _block_text(block: dict[str, Any]) -> str:
    """一个内容块 → 参与计量的文本。什么形状都不许炸(display/key 已是这规矩)。"""
    kind = block.get("type")
    if kind == "text":
        return str(block.get("text", ""))
    if kind == "tool_use":
        # input 才是花 token 的大头;id 是流水号,略过不心疼
        return json.dumps(block.get("input", {}), ensure_ascii=False)
    if kind == "tool_result":
        return str(block.get("content", ""))
    try:
        return json.dumps(block, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(block)


def count_message(msg: dict[str, Any]) -> int:
    """一条消息的估算 token:str 数字符;块列表逐块数;至少 1。"""
    content = msg.get("content")
    if isinstance(content, str):
        return estimate_tokens(content)
    total = 0
    if isinstance(content, list):
        for block in content:
            total += estimate_tokens(_block_text(block))
    return max(total, 1)


def stamp(msg: dict[str, Any], pin: bool, epoch: int) -> Entry:
    """盖章:裸消息 → Entry。pin 由调用方定——只有它还知道这是用户原话还是工具结果。"""
    return Entry(msg=msg, tokens=count_message(msg), pin=pin, epoch=epoch)


def baseline_tokens(system: str, tools: list[dict]) -> int:
    """稳定基线 = 系统提示 + 工具 schema。会话启动算一次、缓存复用;也最吃 prompt-cache。"""
    total = estimate_tokens(system)
    for tool in tools:
        total += estimate_tokens(json.dumps(tool, ensure_ascii=False))
    return total


def make_budget(window: int, max_tokens: int, reserve_ratio: float) -> Budget:
    """config 三个数 → Budget。公式的真相只住 Budget.input_budget 一处。"""
    return Budget(window=window, max_tokens=max_tokens, reserve_ratio=reserve_ratio)


def pressure(entries: list[Entry], baseline: int, budget: Budget) -> float:
    """液位 0~1(可超 1 = 溢出)。(基线 + 历史占用) / 输入预算。调度将来只盯这一个数。"""
    used = baseline + sum(e.tokens for e in entries)
    if budget.input_budget <= 0:
        return 1.0  # 预算已经为负,别让除零炸掉;报满,逼上层动手
    return used / budget.input_budget