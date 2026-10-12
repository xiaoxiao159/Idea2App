"""可复用的单项目 Web 运行时:一个 ProjectServer 实例 = 一条活跃会话的 FastAPI+WebSocket 服务。

把原 backend/server.py 的模块级全局(_loop/_outgoing/_app/_session/_worker/_turns)
收敛成一个类,供三处复用:
  - backend/server.py (单机模式薄壳, python -m backend.server)
  - backend/worker.py (每项目一个子进程, supervisor 拉起)
  - test/smoke_server.py (TestClient 直接驱动)

线程模型(单会话,与原 server.py 完全一致):
  - uvicorn 事件循环线程:WebSocket 收发包;verdict 直接 resolve_current,余者进 turns。
  - 1 个 worker 线程:跑引擎 loop.run(同步阻塞,gate 阻塞在 threading.Event 上)。
  - emit(worker 线程调用):loop.call_soon_threadsafe 塞 asyncio.Queue,唯一 pump task 串行 send_json。

启动约定(WORKDIR gotcha):必须从项目目录起(或先 chdir 再 import agent)。
收到 {"type":"shutdown"} 会触发 uvicorn 优雅退出(供 supervisor 停 worker)。
"""
from __future__ import annotations

import asyncio
import queue
import sys
import threading
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent  # code_agent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402

from Idea2App import bootstrap  # noqa: E402
from backend import bridge  # noqa: E402


class ProjectServer:
    """单会话运行时。config/settings 传 None 时走 bootstrap 的 .env / cwd 默认路径。"""

    def __init__(self, config=None, settings=None, port: int = 8000,
                 frontend_dir: Path | None = None) -> None:
        self.config = config            # None -> bootstrap.setup 里 load_config()
        self.settings = settings        # None -> bootstrap.setup 里 Settings.from_env()
        self.port = port

        self.app = FastAPI(title="Idea2App project")
        self.app.add_api_websocket_route("/ws", self._ws_endpoint)
        if frontend_dir is not None:
            from fastapi.staticfiles import StaticFiles
            self.app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="static")

        # 单会话运行时状态(原 server.py 的模块级全局)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.outgoing: asyncio.Queue | None = None
        self.session: bridge.Session | None = None
        self.app_ctx: bootstrap.App | None = None
        self.worker: threading.Thread | None = None
        self.turns: queue.Queue = queue.Queue()
        self._uvicorn: object | None = None  # uvicorn.Server,shutdown 优雅退出用

    def emit(self, type_: str, **data) -> None:
        """worker 线程调用的线程安全事件发射。"""
        loop, q = self.loop, self.outgoing
        if loop is not None and q is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(q.put_nowait, {"type": type_, **data})
            except RuntimeError:
                pass  # loop 正在关闭

    def build(self) -> None:
        """装配引擎(惰性,首个 WS 连接时触发)。"""
        self.session = bridge.Session(self.emit)
        self.app_ctx = bootstrap.setup(
            config=self.config,
            settings=self.settings,
            ask=self.session.ask,
            pre_hooks=[bridge.make_pre_tool(self.emit)],
            post_hooks=[bridge.make_post_tool(self.emit, self.session)],
        )
        self.session.store = self.app_ctx.store
        bridge.patch_permission(self.emit)
        bridge.wrap_transitions(self.app_ctx.store, self.emit)

    def _worker_loop(self) -> None:
        while True:
            msg = self.turns.get()
            self.emit("turn_start")
            try:
                bridge.handle_turn(self.app_ctx, self.session, self.emit, msg)
            except Exception as exc:  # noqa: BLE001 - 单会话宿主,兜底避免线程死掉
                self.emit("error", message=f"{type(exc).__name__}: {exc}")
            finally:
                self.emit("turn_end")

    def handle_msg(self, msg: dict) -> None:
        if msg.get("type") == "verdict":
            if self.session is not None:
                self.session.resolve_current(bool(msg.get("accept")), str(msg.get("reason") or ""))
        elif msg.get("type") == "shutdown":
            if self._uvicorn is not None:
                self._uvicorn.should_exit = True
        elif msg.get("type") in ("user", "build", "reset"):
            self.turns.put(msg)
        # 其余类型静默忽略

    async def _pump(self, websocket: WebSocket, outgoing: asyncio.Queue) -> None:
        while True:
            ev = await outgoing.get()
            await websocket.send_json(ev)

    def _replay_docs_enqueue(self, outgoing: asyncio.Queue) -> None:
        """把磁盘上已有的产物文档作为 doc 事件重放(重连后恢复视图)。"""
        if self.app_ctx is None:
            return
        stage = self.app_ctx.store.current
        project_dir = self.app_ctx.settings.project_dir
        for fname, kind in (("PRD.md", "prd"), ("SPEC.md", "spec")):
            p = project_dir / fname
            if not p.is_file():
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except OSError:
                continue
            outgoing.put_nowait({
                "type": "doc", "kind": kind, "content": content,
                "status": bridge.doc_status(kind, stage),
                "version": (self.session.version.get(kind) if self.session else 1) or 1,
            })

    async def _ws_endpoint(self, websocket: WebSocket) -> None:
        await websocket.accept()
        if self.app_ctx is None:
            self.build()
        self.loop = asyncio.get_running_loop()
        self.outgoing = asyncio.Queue()
        if self.worker is None:
            self.worker = threading.Thread(
                target=self._worker_loop, daemon=True, name="idea2app-worker")
            self.worker.start()
        pump = asyncio.create_task(self._pump(websocket, self.outgoing))
        try:
            self.outgoing.put_nowait({"type": "hello", "stage": self.app_ctx.store.current.value})
            self._replay_docs_enqueue(self.outgoing)
            while True:
                msg = await websocket.receive_json()
                self.handle_msg(msg)
        except WebSocketDisconnect:
            pass
        finally:
            pump.cancel()
            self.outgoing = None

    def run(self, open_browser: bool = False) -> None:
        import uvicorn

        if open_browser:
            import webbrowser
            threading.Timer(
                0.8, webbrowser.open, args=(f"http://localhost:{self.port}",)).start()
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="info")
        self._uvicorn = uvicorn.Server(config)
        self._uvicorn.run()