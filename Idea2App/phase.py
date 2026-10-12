"""项目生命周期状态机 + Spec 冻结。Idea2App 独有,agent 引擎不认识它(也不需要认识)。

    IDEA → PRD_DRAFT → PRD_APPROVED → SPEC_DRAFT → SPEC_APPROVED(=冻结) → EXECUTING → DONE

两条人审关 = 两个 draft 阶段:写 PRD.md 审产品方向,写 SPEC.md 审技术可执行性。
冻结点 = SPEC_APPROVED:此后 Phase 2 自驱,一律以 SPEC.md 为契约,越界才问人。

状态落盘 .idea2app/phase.json(项目级,进程重启续上)。迁移表写死、严格前向;
返工发生在 draft 内部(靠 gate 的 n 驳回),不需要阶段回退。/reset 是唯一例外缺口
(由宿主显式调用,回 IDEA 并清盘)。
"""
from __future__ import annotations

import json
from enum import Enum
from pathlib import Path


class Stage(str, Enum):
    IDEA = "idea"
    PRD_DRAFT = "prd_draft"
    PRD_APPROVED = "prd_approved"
    SPEC_DRAFT = "spec_draft"
    SPEC_APPROVED = "spec_approved"   # = 冻结,Phase 2 就绪
    EXECUTING = "executing"
    DONE = "done"


# 每个 draft 阶段对应的产物文件:模型 write 这个文件 = 触发那道人审关。
ARTIFACT: dict[Stage, str] = {
    Stage.PRD_DRAFT: "PRD.md",
    Stage.SPEC_DRAFT: "SPEC.md",
}

# 迁移表:严格前向,回退不给。
_TRANSITIONS: dict[Stage, set[Stage]] = {
    Stage.IDEA: {Stage.PRD_DRAFT},
    Stage.PRD_DRAFT: {Stage.PRD_APPROVED},
    Stage.PRD_APPROVED: {Stage.SPEC_DRAFT},
    Stage.SPEC_DRAFT: {Stage.SPEC_APPROVED},
    Stage.SPEC_APPROVED: {Stage.EXECUTING},
    Stage.EXECUTING: {Stage.DONE},
    Stage.DONE: set(),
}


class PhaseError(RuntimeError):
    """非法迁移 / 阶段机被乱动。"""


class PhaseStore:
    """进程内阶段真相 + .idea2app/phase.json 落盘。on_transition 是给外部挂的回调。"""

    def __init__(self, project_dir: Path, on_transition=None):
        self.path = project_dir / ".idea2app" / "phase.json"
        self.stage = self._load()
        self.on_transition = on_transition

    def _load(self) -> Stage:
        if not self.path.is_file():
            return Stage.IDEA
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return Stage(raw["stage"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            return Stage.IDEA  # 坏文件当没有:不炸启动,从 idea 重来

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"stage": self.stage.value}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @property
    def current(self) -> Stage:
        return self.stage

    def transition(self, target: Stage) -> None:
        if target not in _TRANSITIONS.get(self.stage, set()):
            raise PhaseError(f"illegal transition: {self.stage.value} -> {target.value}")
        self.stage = target
        self._save()
        if self.on_transition is not None:
            self.on_transition(self.stage)

    def reset(self) -> None:
        """回到 IDEA。唯一绕过迁移表的出口(宿主 /reset 用)。"""
        self.stage = Stage.IDEA
        self._save()
        if self.on_transition is not None:
            self.on_transition(self.stage)

    def artifact(self) -> str | None:
        """当前阶段若在 draft,返回要审的产物文件名(PRD.md/SPEC.md),否则 None。"""
        return ARTIFACT.get(self.stage)

    @property
    def frozen(self) -> bool:
        """Spec 是否已冻结(Phase 2 契约成立)。"""
        return self.stage in (Stage.SPEC_APPROVED, Stage.EXECUTING, Stage.DONE)