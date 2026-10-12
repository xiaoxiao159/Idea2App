"""工具侧的「当前工作目录」解析。

默认返回进程 cwd;team.py(团队层)在 import 时把它改写成「lead 当前认领任务的工作树」,
这样 lead 认领了绑定 worktree 的任务后,bash/read/write/edit/glob 会自动落进该工作树,
和参考实现 s15 的 run_agent_* 路由一致。没认领任务时退回进程 cwd,绝不把工具弄哑。
"""
from __future__ import annotations

from pathlib import Path

_resolver = lambda: Path.cwd()  # noqa: E731


def resolve_cwd() -> Path:
    """文件/shell 工具应写入的基准目录。"""
    return _resolver()


def set_cwd_resolver(resolver) -> None:
    """team.py 注入:让 lead 的工具 cwd 跟随 assignment_cwd('agent')。"""
    global _resolver
    _resolver = resolver