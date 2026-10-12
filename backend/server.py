"""Idea2App web 单机模式薄壳:一个 ProjectServer + 静态托管 + uvicorn。

多项目工作台见 backend/supervisor.py —— 它拉起每项目一个 backend/worker.py 子进程
(子进程各自跑一个 ProjectServer)。单机模式保留给 python -m backend.server 与冒烟测试。

启动(务必从项目目录起,WORKDIR gotcha):
  cd d:/AI/code_agent && python -m backend.server
端口 IDEA2APP_PORT(默认 8000),权限 IDEA2APP_PERMISSION 可覆盖。
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent  # code_agent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from Idea2App import bootstrap  # noqa: E402  # 暴露给 test/smoke_server.py monkeypatch
from backend.project_server import ProjectServer  # noqa: E402

PORT = int(os.environ.get("IDEA2APP_PORT", "8000"))
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

_server = ProjectServer(port=PORT, frontend_dir=FRONTEND)
app = _server.app  # test/smoke_server.py 用 TestClient(server.app)


def serve(port: int = PORT, open_browser: bool = False) -> None:
    import uvicorn

    url = f"http://localhost:{port}"
    print(f"Idea2App web 已就绪 -> {url}  (Ctrl+C 停止)")
    if open_browser:
        import webbrowser
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    serve(open_browser=not os.environ.get("IDEA2APP_NO_BROWSER"))