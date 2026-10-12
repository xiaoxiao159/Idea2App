"""task 包:跨会话的任务图,multi-agent 协作的地基。

跟 memory/ 的分工:memory 沉"约定/事实/会话",这里沉"把目标拆成的工作单元"
(单元之间的依赖、各自的状态与归属)。落盘 .tasks/{id}.json,活过进程。

工具薄壳住 tools/tasks.py;逻辑与状态住本包 store.py。
"""
from __future__ import annotations

from agent.tasks.store import TASKS, Task, TaskStore

__all__ = ["TASKS", "Task", "TaskStore"]