"""supervisor 的浏览器 WS 端点冒烟(前端连的就是这个 /ws)。

与 smoke_supervisor.py 的区别:那个直驱 Supervisor 协程,这个走 real ASGI 的 browser_ws,
用 TestClient.websocket_connect 验证 hello 结构 + create/start/forward 的事件流
(带 project_id 路由)。同样 IDEA2APP_FAKE_MODEL=1 打假模型。

跑法(仓库根):D:/miniconda/envs/claude/python.exe test/smoke_ws.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

tmp = Path(tempfile.mkdtemp())
os.environ["IDEA2APP_DIR"] = str(tmp / "home")
os.environ["IDEA2APP_SKILLS_DIR"] = str(tmp / "skills")
(tmp / "skills").mkdir()
os.environ["IDEA2APP_FAKE_MODEL"] = "1"
os.environ["IDEA2APP_PORT"] = "0"

from backend import supervisor  # noqa: E402

client = TestClient(supervisor.app)


def recv_type(ws, wanted, timeout=40.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = ws.receive_json()
        if ev.get("type") == wanted:
            return ev
    raise AssertionError(f"timeout waiting for {wanted}")


def main():
    proj_dir = tmp / "proj"
    proj_dir.mkdir()

    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello.get("mode") == "supervisor", hello
        assert "config" in hello and "projects" in hello
        print("ok - hello carries config + projects")

        ws.send_json({"type": "create_project", "name": "demo", "dir": str(proj_dir)})
        ev = recv_type(ws, "projects")
        assert len(ev["projects"]) == 1, ev
        pid = ev["projects"][0]["id"]
        assert ev["projects"][0]["name"] == "demo"
        print("ok - create_project -> projects broadcast")

        ws.send_json({"type": "start_project", "project_id": pid})
        ev = recv_type(ws, "project_status", 60)
        while ev.get("status") != "running":
            ev = recv_type(ws, "project_status", 30)
            if ev.get("project_id") == pid:
                break
        # 收 phase(running 后 supervisor 补发)与 running 状态
        print("ok - start_project reaches running")

        ws.send_json({"type": "user", "text": "hi", "project_id": pid})
        ev = recv_type(ws, "assistant", 40)
        assert ev.get("project_id") == pid, ev
        assert "fake ok" in ev.get("text", ""), ev
        print("ok - forwarded user -> assistant with project_id")

        ws.send_json({"type": "stop_project", "project_id": pid})
        ev = recv_type(ws, "project_status", 30)
        while ev.get("project_id") != pid or ev.get("status") != "stopped":
            ev = recv_type(ws, "project_status", 30)
        print("ok - stop_project -> stopped")

    print("\nALL ws-endpoint assertions passed")


if __name__ == "__main__":
    main()