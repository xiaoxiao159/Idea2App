"""后台任务:慢 bash 命令丢进 daemon 线程,主循环不阻塞。

s11 语义:只有 bash 且显式 run_in_background=true 才走后台;工具调用先回一个
占位 tool_result(带 bg_id),后续轮次 collect() 把完成结果以 <task_notification>
形式注入对话(并进上一条 user 消息,不制造连续两条 user)。

后台任务进程内短命(内存 manager),进程死即亡 —— 这一点和 s10 落盘的 .tasks 相反。
"""
from __future__ import annotations

import atexit
import os
import subprocess
import threading
from typing import Any

from agent.tools.bash import BASH

BASH_TIMEOUT = 120

# 活着的后台进程,退出时统一清理。Windows 没有 killpg/信号组,只 kill 单个进程。
_RUNNING: set[subprocess.Popen] = set()
_LOCK = threading.RLock()


def _run_command(command: str) -> tuple[str, int | None]:
    """一条命令的同步执行体,跑在后台线程里。返回 (output, exit_code)。"""
    process = None
    try:
        process = subprocess.Popen(
            [BASH, "-c", command],
            cwd=os.getcwd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        with _LOCK:
            _RUNNING.add(process)
        stdout, stderr = process.communicate(timeout=BASH_TIMEOUT)
        out = (stdout + stderr).strip()[:10000]
        return (out if out else "(no output)"), process.returncode
    except subprocess.TimeoutExpired:
        return "Error: timeout (120s)", None
    except OSError as exc:
        return f"Error: {exc}", None
    finally:
        if process is not None:
            with _LOCK:
                _RUNNING.discard(process)


def _stop_all() -> None:
    with _LOCK:
        processes = list(_RUNNING)
    for process in processes:
        try:
            process.kill()
        except OSError:
            pass


def _handle_sigterm(signum: int, _frame: Any) -> None:
    _stop_all()
    raise SystemExit(128 + signum)


atexit.register(_stop_all)
try:
    import signal

    signal.signal(signal.SIGTERM, _handle_sigterm)
except (ImportError, ValueError, OSError):
    pass  # 非主线程 / 无信号的环境:atexit 兜底


class BackgroundManager:
    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}
        self.results: dict[str, str] = {}
        self._ready: list[str] = []
        self._counter = 0
        self._lock = threading.Lock()

    def start(self, command: str) -> str:
        with self._lock:
            self._counter += 1
            task_id = f"bg_{self._counter:04d}"
            self.tasks[task_id] = {"command": command, "status": "running"}
        thread = threading.Thread(target=self._run, args=(task_id, command), daemon=True)
        try:
            thread.start()
        except Exception:
            with self._lock:
                self.tasks.pop(task_id, None)
            raise
        return task_id

    def _run(self, task_id: str, command: str) -> None:
        try:
            output, exit_code = _run_command(command)
            status = "completed" if exit_code == 0 else "failed"
            result = (
                output
                if exit_code in (0, None)
                else f"Error: command exited with status {exit_code}\n{output}"
            )
        except Exception as exc:
            result = f"Error: {type(exc).__name__}: {exc}"
            status = "failed"

        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return
            task["status"] = status
            self.results[task_id] = result
            self._ready.append(task_id)

    def collect(self) -> list[str]:
        with self._lock:
            ready = []
            for task_id in self._ready:
                task = self.tasks.pop(task_id, None)
                result = self.results.pop(task_id, "")
                if task is not None:
                    ready.append((task_id, task, result))
            self._ready.clear()

        notifications = []
        for task_id, task, result in ready:
            notifications.append(
                f"<task_notification>\n"
                f"  <task_id>{task_id}</task_id>\n"
                f"  <status>{task['status']}</status>\n"
                f"  <command>{task['command']}</command>\n"
                f"  <summary>{result[:500]}</summary>\n"
                f"</task_notification>"
            )
        return notifications


BACKGROUND = BackgroundManager()


def should_run_background(tool_name: str, tool_input: dict[str, Any]) -> bool:
    return tool_name == "bash" and tool_input.get("run_in_background") is True


def start_background_task(command: str) -> str:
    return BACKGROUND.start(command)


def collect_background_results() -> list[str]:
    return BACKGROUND.collect()


def any_running() -> bool:
    """还有后台任务没跑完(未 collect 且 status=running)。

    goal 评估前用它判断该不该 defer——后台重要结果还没回传,现在判是拿半截证据下结论。
    """
    with BACKGROUND._lock:
        return any(t.get("status") == "running" for t in BACKGROUND.tasks.values())