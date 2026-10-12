"""
挑选:在窗口里挑出"可以被归档的回合",并排好先后。

纯函数——吃 entries,吐 Turn 序号,不碰 WINDOW/ARCHIVE 状态。
第 0/1 档共用;第 2 档摘要也复用同一套护栏。

三条铁律,顺序不可倒(任何裁剪动作都得先过这道闸):
  1. pin=True 绝不碰(用户原话 / 护栏注入);
  2. 进行中的回合绝不碰(工具还没回结果);
  3. 最新尾回合不碰(recency:窗口尾部留最近的原文)。
外加一条:归档占位(以及将来的旧摘要)是"已加工"产物,不反复归档。
"""
from __future__ import annotations

from dataclasses import dataclass

from agent.context.base import Entry


@dataclass(frozen=True)
class Turn:
    """
    一轮对话
    raw_turn,仅对话
    tool_use+toolresult
    """

    start: int
    end: int
    kind: str   # "tool_result" | "raw_turn"(| "summary",第 2 档)
    tokens: int


def _blocks(e: Entry) -> list[dict]:
    content = e.msg.get("content")
    return content if isinstance(content, list) else []


def _has(e: Entry, kind: str) -> bool:
    return any(b.get("type") == kind for b in _blocks(e))


def _synthetic(e: Entry) -> bool:
    """pin=False 且 content 是纯 str = 归档占位/旧摘要(已加工过),别反复归档。"""
    return (not e.pin) and isinstance(e.msg.get("content"), str)


def _resolved_ids(entries: list[Entry]) -> set:
    """已回传结果的 tool_use id。tool_use 有 id 不在这集合 = 仍进行中。"""
    return {
        b.get("tool_use_id")
        for e in entries
        for b in _blocks(e)
        if b.get("type") == "tool_result"
    }


def turns(entries: list[Entry]) -> list[Turn]:
    """把窗口切成原子回合。只有 assistant 能起头;紧邻的 tool_result 一并圈进。"""
    out: list[Turn] = []
    i, n = 0, len(entries)
    while i < n:
        e = entries[i]
        if e.msg["role"] != "assistant":
            i += 1
            continue
        end = i
        if i + 1 < n and entries[i + 1].msg["role"] == "user" and _has(entries[i + 1], "tool_result"):
            end = i + 1
        kind = "tool_result" if end > i else "raw_turn"
        tokens = sum(x.tokens for x in entries[i : end + 1])
        out.append(Turn(start=i, end=end, kind=kind, tokens=tokens))
        i = end + 1
    return out


#进行中的turn，有tool use ，没有tool result
def _in_flight(turn: Turn, entries: list[Entry], resolved: set) -> bool:
    """回合里还有 tool_use 没等到结果 = 进行中,拆不得。"""
    for k in range(turn.start, turn.end + 1):
        for b in _blocks(entries[k]):
            if b.get("type") == "tool_use" and b.get("id") not in resolved:
                return True
    return False


# 谁先归档:越小越先被请出窗口。tool_result 最大最可丢 → 先;旧摘要收益低 → 最后。
_RANK = {"tool_result": 0, "raw_turn": 1, "summary": 2}


#挑选
def pick(entries: list[Entry]) -> list[Turn]:
    """返回"该归档的回合",最该先的排前面;空 = 没有可安全归档的。"""
    ts = turns(entries)
    if not ts:
        return []
    resolved = _resolved_ids(entries)
    last = ts[-1]  # recency:最新尾回合保留
    chosen: list[Turn] = []
    for t in ts:
        if t is last:
            continue
        if any(entries[k].pin or _synthetic(entries[k]) for k in range(t.start, t.end + 1)):
            continue
        if _in_flight(t, entries, resolved):
            continue
        chosen.append(t)
    chosen.sort(key=lambda t: _RANK.get(t.kind, 9))
    return chosen