"""supervisor + worker 冒烟:spawn / 事件转发 / stop。用 IDEA2APP_FAKE_MODEL 假模型,
打真 localhost WS(worker 是真实子进程 + uvicorn)。不直接起浏览器 WS,改直接驱动
Supervisor 协程并替换 broadcast 收集事件。

跑法(仓库根):D:/miniconda/envs/claude/python.exe test/smoke_supervisor.py
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

tmp = Path(tempfile.mkdtemp())
os.environ["IDEA2APP_DIR"] = str(tmp / "home")
os.environ["IDEA2APP_SKILLS_DIR"] = str(tmp / "skills")
(tmp / "skills").mkdir()
os.environ["IDEA2APP_FAKE_MODEL"] = "1"
os.environ["IDEA2APP_PORT"] = "0"  # supervisor 不吃端口(直驱协程),测试里避免误用

from backend import supervisor  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


async def _wait(events, pred, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(pred(e) for e in events):
            return True
        await asyncio.sleep(0.2)
    raise AssertionError(f"timeout waiting; events={events}")


async def main():
    sup = supervisor.Supervisor()
    events: list[dict] = []

    async def cap(ev):
        events.append(ev)

    sup.broadcast = cap

    proj_dir = tmp / "proj"
    proj_dir.mkdir()

    await sup.create_project({"name": "demo", "dir": str(proj_dir)})
    assert len(sup.projects) == 1
    pid = sup.projects[0]["id"]
    ok("create_project registers project")

    await sup.start_project(pid)
    await _wait(events, lambda e: (e.get("type") == "project_status"
                                   and e.get("status") == "running"), 40)
    ok("worker spawns and reaches running")

    # 转发一轮:user -> worker handle_turn -> assistant(fake ok)
    await sup.forward(pid, {"type": "user", "text": "hi"})
    await _wait(events, lambda e: (e.get("type") == "assistant"
                                   and e.get("project_id") == pid), 30)
    ok("user forwards -> assistant echoes back")

    assert any(e.get("type") == "phase" and e.get("project_id") == pid for e in events)
    ok("worker phase event carries project_id")

    await sup.stop_project(pid)
    await _wait(events, lambda e: (e.get("type") == "project_status"
                                   and e.get("project_id") == pid
                                   and e.get("status") == "stopped"), 20)
    ok("stop -> stopped")

    print(f"\nALL {PASS} assertions passed")


asyncio.run(main())