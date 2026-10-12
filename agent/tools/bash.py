"""bash 工具:schema + handler + Git Bash 定位。命令过滤住在 hooks/permission.py(deny floor)。"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from agent import workspace
from agent.tools.base import Tool

_WSL_STUB_MARKERS = ("system32", "windowsapps")


def _works(bash_path: str) -> bool:
    try:
        r = subprocess.run(
            [bash_path, "-c", "echo ok"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=15,
        )
        return r.returncode == 0 and "ok" in r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def _find_bash() -> str:
    candidates: list[str] = [
        r"C:\Program Files\Git\bin\bash.exe",      
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    found = shutil.which("bash")                 
    if found and not any(m in found.lower() for m in _WSL_STUB_MARKERS):
        candidates.append(found)

    for path in candidates:
        if os.path.isfile(path) and _works(path):
            return path
    raise RuntimeError(
        "No working bash found (tried: " + ", ".join(candidates)
        + "). Install Git for Windows."
    )


BASH = _find_bash()  


def _run_bash(params: dict[str, Any]) -> tuple[str, bool]:
    command = params["command"]

    try:
        r = subprocess.run(#subprocess是同步阻塞调用
            [BASH, "-c", command],
            cwd=workspace.resolve_cwd(),
            capture_output=True,
            encoding="utf-8",  # 中文 Windows 默认 gbk,不指定必乱码
            errors="replace",
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return ("Error: timeout (120s)", True)
    except OSError as exc:
        return (f"Error: {exc}", True)

    # stderr 不是异常是信息,合流给模型;截 10000 字符防巨输出撑爆上下文
    out = (r.stdout + r.stderr).strip()[:10000]
    return (out if out else "(no output)", False)


def _display(params: dict[str, Any]) -> str:
    return f"bash$ {params.get('command', '')}"


BASH_TOOL = Tool(
    schema={
        "name": "bash",
        "description": (
            "Run a shell command in Git Bash and return its output. "
            "Set run_in_background=true only for slow, independent commands; "
            "the result then arrives as a task_notification on a later turn."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "run_in_background": {
                    "type": "boolean",
                    "description": "true = run in background, get the result later",
                },
            },
            "required": ["command"],
        },
    },
    handler=_run_bash,
    display=_display,
)
