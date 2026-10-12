"""文档审批门:Phase 1 的两个人审关。挂在 agent 已暴露的 hook 挂载点上,agent 不动。

- check(PreToolUse):拦「write 到当前 draft 产物(PRD.md/SPEC.md)」这一下,渲染全文问人。
  y = 放行写盘 + 推进阶段;n = 否决串退回,模型返工再写。
- on_stop(Stop):PRD 刚获批、还没产 SPEC 时,自动跨进 SPEC_DRAFT 并给指令;
  SPEC 刚获批则交还控制权(冻结,等 /build)。

两条关语义不同但机制同构 = 同一个「文档审批」原语(本文件)+ 两道审查视角
(PRD 审产品方向 / SPEC 审技术可执行),由各自阶段的产物文件名区分。

配套(阶段 → 授权同步):phase 一进 draft 就把 write/edit 预授权进 permission 的会话
授权集,否则模型写 PRD.md 时 permission 会先问一次「allow write?」,再轮到本门问一次
= double-prompt。EXECUTING 时收回,让 permission 恢复「越界才问」的本职。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from Idea2App.phase import PhaseStore, Stage

_PHASE: PhaseStore | None = None
# 可注入的「人工审查提问器」:(artifact, content) -> None 放行 / str 驳回理由。
# None = 用内置终端版 _ask。web 后端把这里换成「发事件 + 阻塞等浏览器裁决」的桥。
_ASK: object | None = None
_DRAFT_GRANTS = frozenset({"write", "edit"})


def install(store: PhaseStore, ask=None) -> None:
    """挂门:记录阶段真相、把两个 hook 追加进 agent 的挂载点、接上授权同步。重复调用幂等。

    ask=(artifact, content)->None/str:web 壳注入浏览器往返;终端壳不传、走内置 input。
    """
    global _PHASE, _ASK
    _PHASE = store
    if ask is not None:
        _ASK = ask
    store.on_transition = _sync_grants
    _sync_grants(store.current)  # 启动即对齐权限集(重启后阶段可能是 EXECUTING)

    from agent import hooks
    if check not in hooks.PRE_TOOL_USE:
        hooks.PRE_TOOL_USE.append(check)
    if on_stop not in hooks.STOP:
        hooks.STOP.append(on_stop)


# ---- PreToolUse:文档审批门 ----


def check(name: str, params: dict[str, Any]) -> str | None:
    """默许 None / 否决串。只对「write 当前产物」发言,其余一律放行。"""
    store = _PHASE
    if store is None:
        return None
    if name != "write":
        return None
    artifact = store.artifact()
    if artifact is None:
        return None
    if Path(str(params.get("file_path", ""))).name != artifact:
        return None

    verdict = (_ASK or _ask)(artifact, str(params.get("content", "")))
    if verdict is not None:
        return verdict
    store.transition(_approved_stage(store.current))
    return None


def _approved_stage(stage: Stage) -> Stage:
    if stage == Stage.PRD_DRAFT:
        return Stage.PRD_APPROVED
    if stage == Stage.SPEC_DRAFT:
        return Stage.SPEC_APPROVED
    return stage


def _ask(artifact: str, content: str) -> str | None:
    print(f"\n  [review] 请审阅这份 {artifact} 全文:")
    print("  " + "-" * 60)
    print(content)
    print("  " + "-" * 60)
    try:
        answer = input(f"  通过? [y]es / [n]o (Enter=n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = "n"
    if answer == "y":
        return None
    try:
        reason = input("  驳回理由 (Enter=无): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        reason = ""
    why = f": {reason}" if reason else ""
    return f"user rejected {artifact}{why}. Revise it accordingly and call write again."


# ---- Stop:跨阶段自动推进 ----


def on_stop(answer: str, usage: dict[str, int]) -> str | None:
    store = _PHASE
    if store is None:
        return None
    if store.current == Stage.PRD_APPROVED:
        store.transition(Stage.SPEC_DRAFT)
        return (
            "PRD approved. Next, write SPEC.md (the technical implementation "
            "specification) at the project root; it will be reviewed too."
        )
    # SPEC_APPROVED(冻结) / 其他阶段:交还控制权,不 auto 开跑。
    return None


# ---- 阶段 → 授权同步 ----


def _sync_grants(stage: Stage) -> None:
    """draft 阶段把 write/edit 预授权(让本门当唯一审查者);EXECUTING 收回、恢复越界才问。"""
    from agent.hooks import permission  # 迟到 import:避免提早把 agent 拉进加载链

    if stage in (Stage.PRD_DRAFT, Stage.SPEC_DRAFT):
        permission._GRANTS.update(_DRAFT_GRANTS)
    elif stage in (Stage.EXECUTING, Stage.IDEA):
        permission._GRANTS.difference_update(_DRAFT_GRANTS)