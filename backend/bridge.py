"""决策桥 + 权限桥 + 事件观测:把 gate 的同步 input 换成浏览器往返。

核心理念:engine/gate 都在 worker 线程里同步阻塞着等 `input()`。web 要做的,是用
`threading.Event` 把「等人按一个键」换成「等浏览器 WebSocket/HTTP 那条线回来一个裁决」:

  Session.ask 被注入 gate(见 bootstrap.setup(ask=...))。gate 在 PRE_TOOL_USE 拦到
  write PRD.md/SPEC.md 时调它 -> 发 doc(draft) + gate_request 事件 -> 阻塞等裁决 ->
  返回 None(放行写盘+推进阶段)或驳回串(模型返工)。engine 毫不知情,以为在等 input。

  patch_permission 把 agent.hooks.permission._ask 换成同样的机制(Phase 2 越界才问),
  v1 策略默认 auto-allow(记可见事件,权限 UI 留到后面);DENY_FLOOR 在 check() 里、
  跑在这之前,始终拦;环境变量 IDEA2APP_PERMISSION=deny 可整体改判。

缓冲正确性:一个 run_loop 可能拦多次(先 PRD 再 SPEC,乃至驳回重写),每次 ask 都新建
Verdict 阻塞;resolve_current 只解除「当前那次」,天然按序,无需协议层加请求 id。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable

from Idea2App import bootstrap
from Idea2App.phase import Stage

GATE_TIMEOUT = 600.0  # 秒:浏览器无人应答时的安全熔断

_KIND = {"PRD.md": "prd", "SPEC.md": "spec"}


def doc_status(kind: str, stage: Stage | None) -> str:
    """文档在给定阶段下的状态:prd 只有 draft->approved;spec 再有 frozen。"""
    if kind == "prd":
        return "draft" if stage == Stage.PRD_DRAFT else "approved"
    if stage in (Stage.SPEC_APPROVED, Stage.EXECUTING, Stage.DONE):
        return "frozen"
    return "draft" if stage == Stage.SPEC_DRAFT else "approved"


class Verdict:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.accept = False
        self.reason = ""
        self.done = False

    def resolve(self, accept: bool, reason: str = "") -> None:
        self.accept = accept
        self.reason = reason
        self.done = True
        self.event.set()

    def wait(self, timeout: float) -> bool:
        return self.event.wait(timeout)


class Session:
    """单会话的浏览器裁决桥 + 产物文档版本计数。emit 必须线程安全(worker 线程调用)。"""

    def __init__(self, emit: Callable[..., None]):
        self.emit = emit
        self.version = {"prd": 0, "spec": 0}
        self.store: Any = None
        self._cond = threading.Condition()
        self._pending: Verdict | None = None

    # ---- gate 注入点 ----
    def ask(self, artifact: str, content: str) -> str | None:
        kind = _KIND.get(artifact, "prd")
        self.version[kind] += 1
        v = self.version[kind]
        self.emit("doc", kind=kind, content=content, status="draft", version=v)
        self.emit("gate_request", kind=kind, artifact=artifact, content=content)
        verdict = Verdict()
        with self._cond:
            self._pending = verdict
            self._cond.notify_all()
        if verdict.wait(GATE_TIMEOUT):
            if verdict.accept:
                return None
            why = f": {verdict.reason}" if verdict.reason else ""
            return f"user rejected {artifact}{why}. Revise it accordingly and call write again."
        return f"user rejected {artifact} (timeout). Revise it accordingly and call write again."

    # ---- 浏览器裁决 -> 解除阻塞 ----
    def resolve_current(self, accept: bool, reason: str = "") -> None:
        with self._cond:
            verdict = self._pending
        if verdict is not None and not verdict.done:
            verdict.resolve(accept, reason)

    def wait_pending(self) -> "Verdict":
        """测试/外部:阻塞直到出现一个待裁决的 gate。"""
        with self._cond:
            while self._pending is None or self._pending.done:
                self._cond.wait()
            return self._pending

    # ---- write 落盘后的 doc 定稿 ----
    def doc_written(self, fname: str, content: str) -> None:
        kind = _KIND.get(fname)
        if kind is None:
            return
        stage = self.store.current if self.store is not None else None
        self.emit("doc", kind=kind, content=content,
                  status=doc_status(kind, stage), version=self.version.get(kind) or 1)

    def reset(self) -> None:
        self.version = {"prd": 0, "spec": 0}
        with self._cond:
            self._pending = None


def _summarize(name: str, params: dict | None) -> str:
    p = params or {}
    if name == "write":
        return str(p.get("file_path", "?"))
    if name in ("edit", "read"):
        return str(p.get("file_path", "?"))
    if name == "bash":
        return str(p.get("command", ""))[:80]
    s = str(p)
    return (s[:80] + "…") if len(s) > 80 else s


def make_pre_tool(emit: Callable[..., None]):
    def pre(name: str, params: dict) -> str | None:
        emit("tool_call", name=name, arg=_summarize(name, params))
        return None
    return pre


def make_post_tool(emit: Callable[..., None], session: Session):
    def post(name: str, params: dict, result: str, is_error: bool) -> None:
        emit("tool_result", ok=not is_error, preview=(result or "")[:200])
        if name == "write":
            fname = Path(str(params.get("file_path", ""))).name
            if fname in _KIND:
                session.doc_written(fname, str(params.get("content", "")))
    return post


def permission_policy() -> str:
    raw = (os.environ.get("IDEA2APP_PERMISSION") or "allow").strip().lower()
    return raw if raw in ("allow", "deny") else "allow"


def patch_permission(emit: Callable[..., None]) -> None:
    """把 engine 的 permission._ask 换成「事件可见 + 按策略应答」。deny 拦不住 DENY_FLOOR。"""
    from agent.hooks import permission

    def _web_ask(shown: str) -> str:
        policy = permission_policy()
        emit("permission", shown=shown, policy=policy)
        return "a" if policy == "allow" else "n"

    permission._ask = _web_ask


def wrap_transitions(store, emit: Callable[..., None]) -> None:
    """在既有 on_transition(gate 的授权同步)之上,追加 phase 事件发射。"""
    prev = store.on_transition

    def on_transition(stage) -> None:
        if callable(prev):
            prev(stage)
        emit("phase", stage=stage.value)

    store.on_transition = on_transition


def handle_turn(app, session: Session, emit: Callable[..., None], msg: dict) -> None:
    """worker 线程内处理一条(非 verdict 的)命令。run() 是同步阻塞的,gate 内的
    阻塞由 dispatcher 线程的 resolve_current 解除。"""
    t = msg.get("type")

    if t == "reset":
        app.store.reset()
        session.reset()
        emit("cleared", {})
        emit("phase", stage=app.store.current.value)
        return

    if t == "build":
        if app.store.current == Stage.SPEC_APPROVED:
            app.store.transition(Stage.EXECUTING)
            answer, usage = app.run(bootstrap.BUILD_INSTRUCTION)
            emit("assistant", text=answer)
            emit("usage", input=usage["input"], output=usage["output"])
        elif app.store.current == Stage.EXECUTING:
            emit("error", message="already executing")
        else:
            emit("error", message="还没有冻结的 SPEC,先走完 Phase 1")
        return

    text = str(msg.get("text") or "").strip()
    if not text:
        return
    if app.store.current == Stage.IDEA:
        app.store.transition(Stage.PRD_DRAFT)
        answer, usage = app.run(bootstrap.prd_instruction(text))
    else:
        answer, usage = app.run(text)
    emit("assistant", text=answer)
    emit("usage", input=usage["input"], output=usage["output"])