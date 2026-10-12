"""档案 = 被请出窗口、但没删掉的原件。内容落盘(cwd/.idea2app/archive/),按会话亡。

和窗口的分工:窗口是"模型正在读的少量原文",档案是"读不动了搬去磁盘存的原文"。
RAM 里只留索引(Block:kind/tokens/path),原件不常驻内存——磁盘近于免费,RAM 才是稀缺的。
fetch 只在本会话内捞回(读文件);跨会话的 recall 是 memory 的事,别在这里混。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent import workspace
from agent.context.base import Block

ARCHIVE: list[Block] = []  # 索引;内容在磁盘
_seq = 0

_DIR = ".idea2app" + "/" + "archive"  # 会话内沉降目录(基准见 _file)


def _relpath(kind: str, seq: int) -> str:
    return f"{_DIR}/{kind}_{seq:04d}.txt"


def _file(rel: str) -> Path:
    # 基准跟着 workspace.resolve_cwd() 走,和 read/write/edit/bash 同源;
    # 裸 Path.cwd() 会在 worktree 任务里与工具 cwd 分叉,把「read 回看占位路径」弄坏。
    return workspace.resolve_cwd() / rel


def push(content: str, tokens: int, kind: str) -> Block:
    """原件落盘 + 入索引,返回这块(调用方若想记住引用)。"""
    global _seq
    _file(_DIR).mkdir(parents=True, exist_ok=True)
    _seq += 1
    rel = _relpath(kind, _seq)
    _file(rel).write_text(content, encoding="utf-8")
    block = Block(kind=kind, tokens=tokens, path=rel)
    ARCHIVE.append(block)
    return block


def read(block_or_rel: Block | str) -> str:
    """取回原件内容。传 Block 或路径字符串都行"""
    rel = block_or_rel.path if isinstance(block_or_rel, Block) else str(block_or_rel)
    try:
        return _file(rel).read_text(encoding="utf-8")
    except OSError:
        return f"(archive unreadable: {rel})"


def fetch(kind: str | None = None, keyword: str | None = None, limit: int = 10) -> list[Block]:
    """会话内召回:按 kind / 子串过滤(读文件比对),最新优先(倒序扫)。"""
    out: list[Block] = []
    for b in reversed(ARCHIVE):
        if kind is not None and b.kind != kind:
            continue
        if keyword is not None and keyword not in read(b):
            continue
        out.append(b)
        if len(out) >= limit:
            break
    return out


def discard(blocks: list[Block]) -> None:
    """折叠进摘要后清掉这些原件:索引 + 盘上文件一起丢。

    摘要已替它们扛下了内容,留着只是磁盘与 fetch 的噪声。按 id 判等,
    不靠 Block.__eq__ —— 同一个路径的对象在别处还有引用时不该被误伤。
    """
    drop = {id(b) for b in blocks}
    ARCHIVE[:] = [b for b in ARCHIVE if id(b) not in drop]
    for b in blocks:
        try:
            _file(b.path).unlink()
        except OSError:
            pass


def clear() -> None:
    """清索引 + 删盘上旧件。reset() 调用 = 新会话从空档案起。"""
    global _seq
    ARCHIVE.clear()
    _seq = 0
    d = _file(_DIR)
    if d.is_dir():
        for f in d.iterdir():
            try:
                if f.is_file():
                    f.unlink()
            except OSError:
                pass


def size() -> int:
    return len(ARCHIVE)


def tokens() -> int:
    """档案里总共躺了多少 token 的原文(观测用)。"""
    return sum(b.tokens for b in ARCHIVE)


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def flatten(msg: dict[str, Any]) -> str:
    """拍平一条消息为可读纯文本:文本块取 text,工具调用/结果打上可读标签。"""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for b in content:
            kind = b.get("type")
            if kind == "text":
                parts.append(str(b.get("text", "")))
            elif kind == "tool_use":
                parts.append(f"[tool] {b.get('name', '?')}({_json(b.get('input', {}))})")
            elif kind == "tool_result":
                parts.append(str(b.get("content", "")))
            else:
                parts.append(_json(b))
    return "\n".join(parts)


def placeholder(kind: str, tokens_num: int, path: str) -> dict[str, Any]:
    """窗口里替掉被归档回合的迷你占位。role 恒 assistant,content 是纯 str。

    path 一并写进占位:模型要回看细节时,用 read 工具读这个文件即可——这就是
    模型侧的"fetch"(不再单开一个 fetch 工具,复用现有的 read + 落盘路径)。
    """
    label = {
        "tool_result": "一个工具回合",
        "raw_turn": "一段助手回复",
        "summary": "一串旧摘要",
    }.get(kind, "一批内容")
    return {
        "role": "assistant",
        "content": f"[归档] {label}(原 {tokens_num:,} tok 已入档案 {path};回看用 read 读该文件)",
    }