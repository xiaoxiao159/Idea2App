"""分级 compact 的调度:观察 → 归档 → 摘要 → 保命。

每档做完重测压力,压到 TARGET 就收手——能不用 LLM 就不用,贵的档永远最后动。
四档全落地:第 0 档观察、第 1 档归档(落盘,无损)、第 2 档摘要(碰 LLM,
把已归档的原文折叠成一段要点)、第 3 档保命(纯机械,零 LLM)。

两个入口:
  compact(model) —— 阈值驱动:压力 ≥ SAFE 才动手(loop 每次开口前调用)。
  force(model)   —— 无条件:能归档的全归档 + 保命。compact 工具(模型主动喊)
                   与 prompt_too_long 救急共用这一个动作。
"""
from __future__ import annotations

from typing import Any

from agent import context
from agent.context import archive, meter, select

SAFE = 0.85            # 触发线:压力到这,走梯子
TARGET = 0.80          # 目标水位:压到低于它,收手
MIN_SAVE = 2_000       # 摘要价值门槛(第 2 档用):批次省不满这些 token 不值得烧 LLM
TRUNC_LIMIT = 10_000   # 保命(第 3 档):单条 tool_result 超这个字符数就硬截

# ---- 第 2 档摘要:碰 LLM,把已归档原文折叠成一段自包含要点 ----
SUMMARIZE_SYSTEM = (
    "你是会话压缩器。把下面这些已被归档的历史工作回合,浓缩成一段自包含的要点摘要。"
    "保留:任务目标、已做的关键决定与理由、得出的结论、仍在进行/未解决的问题、下一步。"
    "丢弃:纯执行过程、命令回显、模板式输出。用中文,简洁,不超过 500 字。"
)
SUMMARIZE_MAX_INPUT_CHARS = 30_000  # 一次喂给 LLM 的归档原文上限(超了截头,保底不炸)
SUMMARIZE_MAX_TOKENS = 2_000        # 摘要输出上限:摘要本身就是省 token 的,别反过来吃满

#入口/阈值
def compact(model: Any = None) -> None:
    """阈值驱动入口:没 setup 或压力没到 SAFE 就什么都不做。"""
    if context._BASELINE is None or context._BUDGET is None:
        return  # 没 setup(或测试只 reset),不动

    # 第 0 档:观察
    if not _over(SAFE):
        return

    _run_ladder(model, stop_at_target=True)

#入口/保底
def force(model: Any = None) -> None:
    """无条件入口:无视 SAFE,能归档的全归档 + 保命。救急/主动都用它。"""
    if context._BASELINE is None or context._BUDGET is None:
        return
    _run_ladder(model, stop_at_target=False)


def _pressure() -> float:
    return meter.pressure(context.WINDOW, context._BASELINE, context._BUDGET)


def _over(line: float) -> bool:
    return _pressure() >= line


def _under_target() -> bool:
    return _pressure() < TARGET

#1
def _run_ladder(model: Any, stop_at_target: bool) -> None:
    _archive_pass(stop_at_target)
    if _under_target():
        return
    # 第 2 档:摘要(下一节;现在恒 False → 降级)
    if not _summarize_pass(model):
        # 第 3 档:保命
        _truncate_pass()

#档1 归档
def _archive_pass(stop_at_target: bool) -> int:
    """第 1 档:每次只搬一个回合,搬完重挑重测——下标才不会因删插而错位。

    stop_at_target=True 压到 TARGET 就停(阈值入口);
    False 则一直搬到没得搬(force:救急要榨干能无损榨的空间)。
    """
    moved = 0
    while True:
        if stop_at_target and _under_target():
            break
        turns = select.pick(context.WINDOW)
        if not turns:
            break
        _archive_turn(turns[0])
        moved += 1
    return moved

#档1 
def _archive_turn(turn: select.Turn) -> None:
    """把一个回合搬进档案(落盘)、窗口留带路径的占位。定位→拍平→入档→请出→轮次+1。"""
    win = context.WINDOW
    members = win[turn.start : turn.end + 1]
    text = "\n\n".join(archive.flatten(m.msg) for m in members)
    tokens_num = sum(m.tokens for m in members)
    block = archive.push(text, tokens_num, turn.kind)#入盘

    ph = meter.stamp(
        archive.placeholder(turn.kind, tokens_num, block.path),
        pin=False,
        epoch=context._EPOCH,
    )
    del win[turn.start : turn.end + 1]
    win.insert(turn.start, ph)
    context.note_archival()  # 轮次 +1:新条目落进新的 epoch

#归2 压缩
def _summarize_pass(model: Any) -> bool:
    """第 2 档:把已归档的原文折叠成一段摘要,替掉窗口里的归档占位。

    成功 → True(调度不再降级到保命);没原文 / 没模型 / 价值不够 / LLM 失败
    → False,让调度降级到保命档。摘要只对"还没摘要过的原始块"下手,摘要块不反复摘要。
    """
    raw = [b for b in archive.ARCHIVE if b.kind != "summary"]
    if not raw or model is None:
        return False
    if sum(b.tokens for b in raw) < MIN_SAVE:
        return False
    text = "\n\n---\n\n".join(archive.read(b) for b in raw)
    if len(text) > SUMMARIZE_MAX_INPUT_CHARS:
        text = text[:SUMMARIZE_MAX_INPUT_CHARS]
    summary = _llm_summarize(model, text)
    if not summary:
        return False
    _fold_into_summary(summary)
    archive.discard(raw)  # 原文已被摘要扛下,清掉索引与盘上文件
    return True


def _llm_summarize(model: Any, text: str) -> str | None:
    """一次无工具的纯补全,取文本面。失败/空 → None。"""
    try:
        resp = model.complete(
            [{"role": "user", "content": text}], SUMMARIZE_SYSTEM, [], SUMMARIZE_MAX_TOKENS
        )
    except Exception:
        return None
    out = "\n".join(
        b.get("text", "") for b in (resp.get("content") or []) if b.get("type") == "text"
    ).strip()
    return out or None


def _fold_into_summary(summary_text: str) -> None:
    """窗口里所有"已加工"的 str 条目(归档占位 + 上一轮的旧摘要)→ 一条新摘要。

    pin=False 的纯 str 只可能有两种来源:归档占位(_archive_turn 造)与旧摘要
    (本函数造)——select._synthetic 正是这个判定。一并折掉,顺带解决"旧摘要
    堆积不可剔除"的问题。真正的原话(pin=True)与块内容(assistant/tool_result)
    原样保留;新摘要插回首个被替位置,不搅乱时间序。
    """
    kept: list[Any] = []
    insert_at = -1
    for e in context.WINDOW:
        if select._synthetic(e):
            if insert_at < 0:
                insert_at = len(kept)
            continue
        kept.append(e)
    entry = meter.stamp(
        {"role": "assistant", "content": summary_text}, pin=False, epoch=context._EPOCH
    )
    kept.insert(insert_at if insert_at >= 0 else 0, entry)
    context.WINDOW[:] = kept


#档3 硬拦截
def _truncate_pass() -> int:
    """第 3 档:单条超大 tool_result 硬截,pin 的绝不截(截了即丢原话)。"""
    count = 0
    for idx, e in enumerate(context.WINDOW):
        if e.pin:
            continue
        blocks = e.msg.get("content")
        if not isinstance(blocks, list):
            continue
        if not any(b.get("type") == "tool_result" for b in blocks):
            continue
        new_blocks = list(blocks)
        changed = False
        for b in new_blocks:
            if b.get("type") != "tool_result":
                continue
            text = str(b.get("content", ""))
            if len(text) > TRUNC_LIMIT:
                b["content"] = text[:TRUNC_LIMIT] + f"\n[truncated] 其余 {len(text) - TRUNC_LIMIT:,} 字符已丢弃"
                changed = True
        if changed:
            context.WINDOW[idx] = meter.stamp(
                {"role": e.msg["role"], "content": new_blocks}, pin=False, epoch=e.epoch
            )
            count += 1
    return count