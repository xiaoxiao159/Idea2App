from __future__ import annotations

from typing import Any

from agent.tools.base import Tool

STATUSES = ("pending", "in_progress", "completed")
MAX_ITEMS = 20
MAX_CONTENT_CHARS = 200

_MARKS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}

# 会话状态:当前清单。进程退出即消失。
_TODOS: list[dict] = []


def validate(todos: Any) -> str | None:
    """校验提案。合法 → None;非法 → 错误文本。gate 和 handler 共用:先验后问。"""
    if not isinstance(todos, list):
        return "Error: todos must be an array"
    if len(todos) > MAX_ITEMS:
        return f"Error: too many items ({len(todos)} > {MAX_ITEMS})"
    seen: set[str] = set()
    in_progress = 0
    for i, item in enumerate(todos, 1):
        if not isinstance(item, dict):
            return f"Error: item {i}: must be an object"
        content = str(item.get("content", ""))
        if not content.strip():
            return f"Error: item {i}: content is empty"
        if len(content) > MAX_CONTENT_CHARS:
            return f"Error: item {i}: content too long (> {MAX_CONTENT_CHARS} chars)"
        if content in seen:
            # diff 按 content 配对,重影会让增删账算错——从门口挡掉
            return f"Error: item {i}: duplicate content '{content}'"
        if item.get("status") not in STATUSES:
            return f"Error: item {i}: status must be one of {STATUSES}, got {item.get('status')!r}"
        seen.add(content)
        if item.get("status") == "in_progress":
            in_progress += 1
    if in_progress > 1:
        return (
            f"Error: at most 1 in_progress item allowed, got {in_progress}; "
            "finish one before starting the next"
        )
    return None


def render(todos: list[dict]) -> str:
    """渲染给人看的清单。ASCII 状态标记:Windows 控制台不吃 Unicode 方块。"""
    lines = [f"{i:>2}. {_MARKS[t['status']]} {t['content']}" for i, t in enumerate(todos, 1)]
    if lines:
        done = sum(1 for t in todos if t["status"] == "completed")
        lines.append(f"({done}/{len(todos)} done)")
    else:
        lines.append("(todo list is empty)")
    return "\n".join(lines)


def _diff(old: list[dict], new: list[dict]) -> tuple[list[str], list[str], list[str]]:
    old_map = {str(t["content"]): t["status"] for t in old}
    new_map = {str(t["content"]): t["status"] for t in new}
    added = [c for c in new_map if c not in old_map]
    removed = [c for c in old_map if c not in new_map]
    changed = [c for c in new_map if c in old_map and new_map[c] != old_map[c]]
    return added, removed, changed


def _write(params: dict[str, Any]) -> tuple[str, bool]:
    global _TODOS
    todos = params.get("todos")
    err = validate(todos)
    if err:
        return (err, True)

    added, removed, changed = _diff(_TODOS, todos)  # 先对旧账,再换新账
    _TODOS = [dict(t) for t in todos]               # 拷贝入账:外部引用别反写内部状态

    if not todos:
        return ("(todo list cleared)", False)
    bits = []
    if added:
        bits.append(f"+{len(added)} added")
    if removed:
        bits.append(f"-{len(removed)} removed")
    if changed:
        bits.append(f"~{len(changed)} status change{'s' if len(changed) > 1 else ''}")
    note = "; ".join(bits) if bits else "identical to previous list"
    return (f"{render(todos)}\n({note})", False)


def _disp_write(params: dict[str, Any]) -> str:
    # display 在校验之前跑(loop 打印、permission/audit 都会调),什么形状都不许炸
    todos = params.get("todos")
    if not isinstance(todos, list):
        return "todo_write (invalid todos)"
    if not todos:
        return "todo_write (clear)"
    done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
    run = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "in_progress")
    return f"todo_write ({len(todos)} items: {done} done, {run} in progress)"


TODO_TOOL = Tool(
    schema={
        "name": "todo_write",
        "description": (
            "Record and update the task plan. Each call REPLACES the entire list: "
            "always send the complete new list, never a patch. "
            "Use for tasks with 3+ steps: propose the plan before starting work, "
            "mark exactly one item in_progress while doing it, "
            "and mark it completed right after."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "the complete todo list (replaces the previous one)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "short task description"},
                            "status": {
                                "type": "string",
                                "enum": list(STATUSES),
                                "description": "pending | in_progress | completed",
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
    },
    handler=_write,
    display=_disp_write,
)
