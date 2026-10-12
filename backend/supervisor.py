"""Idea2App 多项目 supervisor:全局配置 + 项目注册 + 每项目 worker 子进程 + 单 /ws 代理。

浏览器只连 http://localhost:8000/ws;supervisor 负责:
  - 全局 API 配置(registry.ConfigStore)与项目注册表(registry.ProjectStore)
  - 每活跃项目 Popen 一个 backend/worker.py 子进程,作为 WS 客户端连其 /ws
  - 消息代理:浏览器命令(带 project_id)→ worker;worker 事件 → +project_id → 广播浏览器
  - 全局 free-chat:build_config + get_model 直连,tools=[] 只出文本(不 import 引擎重状态)

隔离原则:supervisor 自身**不 import** Idea2App/bootstrap / agent.loop / agent.context
等会拉起约 25+ 个进程级单例的模块;只碰 agent.config.build_config / agent.models.get_model
这两个叶子,以及 backend 的 registry / dirpicker(纯函数)。多项目隔离靠"每项目一子进程"。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent  # code_agent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from agent.config import build_config  # noqa: E402
from agent.models import get_model  # noqa: E402
from backend import dirpicker, registry  # noqa: E402

FRONTEND = _PKG_ROOT / "frontend"
PORT = int(os.environ.get("IDEA2APP_PORT", "8000"))


class WorkerLink:
    """supervisor 与一个 worker 的 WS 客户端连接 + 出站命令队列。"""

    def __init__(self, sup: "Supervisor", project_id: str, ws) -> None:
        self.sup = sup
        self.project_id = project_id
        self.ws = ws
        self.outq: asyncio.Queue = asyncio.Queue()
        self.tasks: tuple = ()

    async def read_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    ev = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                ev["project_id"] = self.project_id
                await self.sup.broadcast(ev)
        except Exception as exc:  # noqa: BLE001 - 连接断开/被 stop 取消
            await self.sup.worker_lost(self.project_id, f"{type(exc).__name__}: {exc}")

    async def write_loop(self) -> None:
        while True:
            msg = await self.outq.get()
            await self.ws.send(json.dumps(msg, ensure_ascii=False))


class Supervisor:
    def __init__(self) -> None:
        self.config_store = registry.ConfigStore()
        self.project_store = registry.ProjectStore()
        self.config = self.config_store.load()
        self.projects = self._load_projects()
        self.links: dict[str, WorkerLink] = {}
        self.browsers: set[WebSocket] = set()
        self.chat_msgs: list[dict] = []
        self.chat_model = None

    # ---- 项目 / 配置 ----
    def _load_projects(self) -> list[dict]:
        projects = self.project_store.load()
        for p in projects:
            p["status"] = "stopped"           # lazy resume:不自动 spawn
            p["port"] = None
            p["last_stage"] = registry.last_stage_of(p["dir"])
        self.project_store.save(projects)
        return projects

    def find(self, project_id):
        return next((p for p in self.projects if p["id"] == project_id), None)

    def projects_public(self) -> list[dict]:
        keys = ("id", "name", "dir", "status", "last_stage", "created")
        return [{k: p.get(k) for k in keys} for p in self.projects]

    def config_public(self) -> dict:
        c = dict(self.config)
        c["has_key"] = bool(c.get("api_key"))
        c.pop("api_key", None)
        return c

    def worker_env(self, proj: dict) -> dict:
        env = dict(os.environ)
        for k in registry.MODEL_ENV_KEYS:
            env.pop(k, None)
        env.update(registry.to_env(self.config))
        env["PYTHONPATH"] = str(_PKG_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUTF8"] = "1"
        return env

    def save_projects(self) -> None:
        self.project_store.save(self.projects)

    # ---- 广播 ----
    async def broadcast(self, ev: dict) -> None:
        dead = []
        for ws in list(self.browsers):
            try:
                await ws.send_json(ev)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.browsers.discard(ws)

    # ---- worker 生命周期 ----
    async def start_project(self, project_id: str) -> None:
        proj = self.find(project_id)
        if proj is None or proj.get("status") in ("running", "starting"):
            return
        proj["status"] = "starting"
        self.save_projects()
        await self.broadcast({"type": "project_status", "project_id": project_id, "status": "starting"})
        try:
            port = registry.pick_port()
            proj["port"] = port
            self.save_projects()
            log_path = Path(proj["dir"]) / ".idea2app" / "worker.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            logf = open(log_path, "ab", buffering=0)
            kwargs: dict = {}
            if os.name == "nt":
                kwargs["creationflags"] = 0x08000000 | 0x00000200  # NO_WINDOW | NEW_PROCESS_GROUP
            proc = subprocess.Popen(
                [sys.executable, "-m", "backend.worker",
                 "--project-dir", proj["dir"], "--port", str(port)],
                cwd=proj["dir"], env=self.worker_env(proj),
                stdout=logf, stderr=subprocess.STDOUT, **kwargs)
            proj["_proc"] = proc
            proj["_logf"] = logf
            asyncio.create_task(self.connect_worker(proj))
        except Exception as exc:
            await self.worker_lost(project_id, f"spawn failed: {type(exc).__name__}: {exc}")

    async def connect_worker(self, proj: dict) -> None:
        pid = proj["id"]
        url = f"ws://127.0.0.1:{proj['port']}/ws"
        import websockets
        ws = None
        deadline = time.time() + 20
        while time.time() < deadline:
            proc = proj.get("_proc")
            if proc and proc.poll() is not None:
                await self.worker_lost(pid, f"worker exited early: code {proc.returncode}")
                return
            try:
                ws = await asyncio.wait_for(websockets.connect(url), timeout=5)
                break
            except Exception:
                await asyncio.sleep(0.3)
        if ws is None:
            await self.worker_lost(pid, "connect timeout")
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            hello = json.loads(raw)
        except Exception as exc:
            try:
                await ws.close()
            except Exception:
                pass
            await self.worker_lost(pid, f"no hello: {type(exc).__name__}")
            return
        proj["status"] = "running"
        self.save_projects()
        link = WorkerLink(self, pid, ws)
        self.links[pid] = link
        await self.broadcast({"type": "project_status", "project_id": pid, "status": "running"})
        await self.broadcast({"type": "phase", "project_id": pid, "stage": hello.get("stage")})
        link.tasks = (
            asyncio.create_task(link.read_loop()),
            asyncio.create_task(link.write_loop()),
        )

    async def stop_project(self, project_id: str) -> None:
        proj = self.find(project_id)
        if proj is None:
            return
        proj["status"] = "stopped"  # 先标意图,防 read_loop 竞态把状态覆盖成 error
        proj["port"] = None
        link = self.links.pop(project_id, None)
        if link is not None:
            try:
                await asyncio.wait_for(link.ws.send(json.dumps({"type": "shutdown"})), timeout=3)
            except Exception:
                pass
            for t in link.tasks:
                t.cancel()
            try:
                await link.ws.close()
            except Exception:
                pass
        await self._reap_proc(proj)
        proj["status"] = "stopped"
        proj["port"] = None
        self.save_projects()
        await self.broadcast({"type": "project_status", "project_id": project_id, "status": "stopped"})

    async def _reap_proc(self, proj: dict) -> None:
        proc = proj.pop("_proc", None)
        logf = proj.pop("_logf", None)
        if proc and proc.poll() is None:
            try:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=5)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        if logf:
            try:
                logf.close()
            except Exception:
                pass

    async def worker_lost(self, project_id: str, reason: str) -> None:
        link = self.links.pop(project_id, None)
        proj = self.find(project_id)
        status = "error"
        if proj is not None:
            if proj["status"] not in ("stopped", "completed"):
                proj["status"] = "error"
            status = proj["status"]
            proj["port"] = None
            self.save_projects()
            await self._reap_proc(proj)
        if link is not None:
            try:
                await link.ws.close()
            except Exception:
                pass
        await self.broadcast({"type": "project_status", "project_id": project_id,
                              "status": status, "detail": reason})

    # ---- 消息处理 / 代理 ----
    async def forward(self, project_id: str, msg: dict) -> None:
        link = self.links.get(project_id)
        if link is None:
            return
        payload = {k: v for k, v in msg.items() if k != "project_id"}
        await link.outq.put(payload)

    async def handle_browser_msg(self, msg: dict) -> None:
        t = msg.get("type")
        if t in ("user", "build", "reset", "verdict"):
            pid = msg.get("project_id")
            if pid is None:
                if t == "reset":
                    self.chat_msgs = []
                    await self.broadcast({"type": "cleared", "project_id": None})
                else:
                    await self.broadcast({"type": "error", "project_id": None,
                                          "message": "请先选择/进入一个项目"})
            else:
                await self.forward(pid, msg)
        elif t == "chat":
            await self.handle_chat(msg)
        elif t == "create_project":
            await self.create_project(msg)
        elif t == "start_project":
            await self.start_project(msg.get("project_id"))
        elif t == "stop_project":
            await self.stop_project(msg.get("project_id"))
        elif t == "mark_complete":
            await self.mark_complete(msg.get("project_id"))
        elif t == "delete_project":
            await self.delete_project(msg.get("project_id"))
        elif t == "pick_dir":
            await self.pick_dir()
        elif t == "set_config":
            await self.set_config(msg.get("config") or {})

    async def pick_dir(self) -> None:
        path = await asyncio.to_thread(dirpicker.pick_dir)
        if path:
            await self.broadcast({"type": "dir_picked", "path": path})
        else:
            await self.broadcast({"type": "dir_picked", "path": None, "cancelled": True})

    async def create_project(self, msg: dict) -> None:
        directory = str(msg.get("dir") or "").strip()
        if not directory:
            await self.broadcast({"type": "error", "message": "项目目录不能为空"})
            return
        name = str(msg.get("name") or "").strip() or Path(directory).name
        proj = registry.new_project(name, directory)
        proj["last_stage"] = registry.last_stage_of(directory)
        self.projects.append(proj)
        self.save_projects()
        await self.broadcast({"type": "projects", "projects": self.projects_public()})

    async def mark_complete(self, project_id: str) -> None:
        proj = self.find(project_id)
        if proj is None:
            return
        if proj.get("status") in ("running", "starting"):
            await self.stop_project(project_id)
            proj = self.find(project_id)
        proj["status"] = "completed"
        self.save_projects()
        await self.broadcast({"type": "projects", "projects": self.projects_public()})

    async def delete_project(self, project_id: str) -> None:
        proj = self.find(project_id)
        if proj is None:
            return
        if proj.get("status") in ("running", "starting"):
            await self.stop_project(project_id)
        self.projects = [p for p in self.projects if p["id"] != project_id]
        self.save_projects()
        await self.broadcast({"type": "projects", "projects": self.projects_public()})

    async def set_config(self, cfg: dict) -> None:
        new = {
            "provider": str(cfg.get("provider") or self.config.get("provider") or "anthropic"),
            "api_key": str(cfg.get("api_key") or self.config.get("api_key") or ""),
            "base_url": str(cfg.get("base_url") or ""),
            "model": str(cfg.get("model") or ""),
            "max_tokens": int(cfg.get("max_tokens") or self.config.get("max_tokens") or 8000),
            "window": int(cfg.get("window") or self.config.get("window") or 200000),
        }
        self.config = new
        self.config_store.save(new)
        self.chat_model = None
        running = [p["id"] for p in self.projects if p["status"] in ("running", "starting")]
        for pid in running:
            await self.stop_project(pid)
        await self.broadcast({"type": "config_updated", "config": self.config_public()})
        for pid in running:
            await self.start_project(pid)
        if running:
            await self.broadcast({"type": "project_status", "project_id": None, "status": "note",
                                  "detail": f"配置已保存,重启了 {len(running)} 个运行中的项目"})

    # ---- 全局 chat ----
    def _ensure_chat_model(self):
        if self.chat_model is None:
            self.chat_model = get_model(build_config(registry.to_env(self.config)))
        return self.chat_model

    async def handle_chat(self, msg: dict) -> None:
        text = str(msg.get("text") or "").strip()
        if not text:
            return
        system = ("你是 Idea2App 的助手:帮用户梳理、打磨应用想法,把模糊需求变清晰。"
                  "你不能写文件或执行命令,只能对话。")
        self.chat_msgs.append({"role": "user", "content": text})
        await self.broadcast({"type": "turn_start", "project_id": None})
        try:
            model = self._ensure_chat_model()
            resp = await asyncio.to_thread(
                model.complete, self.chat_msgs, system, [], int(self.config.get("max_tokens") or 8000))
        except Exception as exc:
            await self.broadcast({"type": "error", "project_id": None,
                                  "message": f"chat: {type(exc).__name__}: {exc}"})
            await self.broadcast({"type": "turn_end", "project_id": None})
            return
        answer = "\n".join(b["text"] for b in resp.get("content", []) if b.get("type") == "text")
        self.chat_msgs.append({"role": "assistant", "content": resp.get("content", [])})
        await self.broadcast({"type": "assistant", "project_id": None, "text": answer})
        usage = resp.get("usage", {})
        await self.broadcast({"type": "usage", "project_id": None,
                              "input": usage.get("input", 0), "output": usage.get("output", 0)})
        await self.broadcast({"type": "turn_end", "project_id": None})


sup = Supervisor()
app = FastAPI(title="Idea2App")


@app.websocket("/ws")
async def browser_ws(websocket: WebSocket) -> None:
    global sup
    await websocket.accept()
    sup.browsers.add(websocket)
    try:
        await websocket.send_json({
            "type": "hello", "mode": "supervisor",
            "config": sup.config_public(),
            "projects": sup.projects_public(),
        })
        while True:
            msg = await websocket.receive_json()
            await sup.handle_browser_msg(msg)
    except WebSocketDisconnect:
        pass
    finally:
        sup.browsers.discard(websocket)


app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="static")


def serve(port: int = PORT) -> None:
    import uvicorn

    print(f"Idea2App 多项目工作台 -> http://localhost:{port}  (Ctrl+C 停止)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    serve()