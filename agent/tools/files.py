from __future__ import annotations

from pathlib import Path
from typing import Any

from agent import workspace
from agent.tools.base import Tool

# ---- 会话状态:本进程读过的文件(规范化绝对路径) ----
READ: set[str] = set()

DEFAULT_LIMIT = 200      
MAX_LINE_CHARS = 2000    


def _resolve(path: str) -> Path:
    """相对路径落 cwd(lead 认领任务的工作树,见 workspace),绝对路径原样;
    resolve 消掉 .. 和符号链接。"""
    # Path 的 / 遇到绝对路径的右操作数会直接取右边——两种输入一行收编
    return (workspace.resolve_cwd() / path).resolve()


def _load_text(path: Path) -> tuple[str | None, str]:
    """读 utf-8 文本,read / edit 共用(这张六行错误表出现两次,复制必漂移)。
    成功 → (text, "");失败 → (None, 错误文本)。"""
    try:
        return path.read_text(encoding="utf-8"), ""
    except FileNotFoundError:
        return None, f"Error: file not found: {path}"
    except IsADirectoryError:
        return None, f"Error: is a directory: {path}"
    except UnicodeDecodeError:
        return None, f"Error: cannot decode as utf-8 (binary or non-utf-8 file): {path}"
    except OSError as exc:
        return None, f"Error: {exc}"


def _save_text(path: Path, text: str) -> str | None:
    """写 utf-8 + newline="\\n"(回路零变形的写侧机关)。成功 → None;失败 → 错误文本。"""
    try:
        with path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return None
    except OSError as exc:
        return f"Error: {exc}"


def _make_parents(path: Path) -> str | None:
    """mkdir -p 语义:写 sub/dir/w.txt 不用模型先跑 mkdir。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return None
    except OSError as exc:
        return f"Error: {exc}"


# ---- read ----

def _read(params: dict[str, Any]) -> tuple[str, bool]:
    path = _resolve(str(params["file_path"]))

    limit = max(1, int(params.get("limit") or DEFAULT_LIMIT))
    offset = max(0, int(params.get("offset") or 0))

    text, err = _load_text(path)
    if err:
        return (err, True)
    READ.add(str(path))          # decode 成功即记(哪怕窗口为空):防盲写的目的已达到

    lines = text.split("\n")
    if lines and lines[-1] == "":    # "a\n".split 出尾部的空元素,不 pop 会虚报一行
        lines.pop()

    total = len(lines)
    window = lines[offset : offset + limit]
    if not window:
        msg = "(empty file)" if total == 0 else f"(no lines at offset={offset}; file has {total} lines)"
        return (msg, False)

    out = []
    for no, line in enumerate(window, start=offset + 1):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + " ... [line truncated]"
        out.append(f"{no:>6}\t{line}")

    if offset + limit < total:
        out.append(f"({total - offset - limit} more lines; use offset={offset + limit} to continue)")

    return ("\n".join(out), False)


# ---- write ----

def _write(params: dict[str, Any]) -> tuple[str, bool]:
    path = _resolve(str(params["file_path"]))
    content = str(params["content"])

    if path.exists() and not path.is_file():
        return (f"Error: path exists and is not a file: {path}", True)

    if path.is_file() and str(path) not in READ:
        return (
            f"Error: file exists but has not been read this session; read it first: {path}",
            True,
        )

    err = _make_parents(path)
    if err:
        return (err, True)
    err = _save_text(path, content)
    if err:
        return (err, True)

    READ.add(str(path))           
    return (
        f"(wrote {path}: {len(content.encode('utf-8'))} bytes, {len(content.splitlines())} lines)",
        False,
    )


# ---- edit ----

def _edit(params: dict[str, Any]) -> tuple[str, bool]:
    path = _resolve(str(params["file_path"]))
    old = str(params["old_string"])
    new = str(params["new_string"])

    # exists 检查放在 _load_text 之前:为了给"用 write 建"指路(建文件是 write 的活)
    if not path.exists():
        return (f"Error: file not found: {path} (use write to create it)", True)

    text, err = _load_text(path)
    if err:
        return (err, True)

    if str(path) not in READ:
        return (f"Error: file has not been read this session; read it first: {path}", True)

    n = text.count(old)
    if n == 0:
        return ("Error: old_string not found; check exact content (whitespace/indentation)", True)
    if n > 1:
        return (f"Error: old_string found {n} times; include surrounding lines to make it unique", True)

    new_text = text.replace(old, new)    # count==1 已证唯一,replace 才敢全文跑
    err = _save_text(path, new_text)
    if err:
        return (err, True)

    return (f"(edited {path}: replaced 1 occurrence, file now {len(new_text)} chars)", False)


# ---- display:给人看的摘要 ----

def _disp_read(params: dict[str, Any]) -> str:
    return f"read {params.get('file_path', '?')}"


def _disp_write(params: dict[str, Any]) -> str:
    return f"write {params.get('file_path', '?')} ({len(str(params.get('content', '')))} chars)"


def _disp_edit(params: dict[str, Any]) -> str:
    old = str(params.get("old_string", "")).replace("\n", "\\n")   # 多行 old 别把终端搞乱
    if len(old) > 50:
        old = old[:50] + "..."
    return f"edit {params.get('file_path', '?')}: {old}"


READ_TOOL = Tool(
    schema={
        "name": "read",
        "description": (
            "Read a utf-8 text file with line numbers. "
            "Returns the first 200 lines by default; use offset/limit for longer files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "file path, relative to cwd or absolute"},
                "limit": {"type": "integer", "description": "max lines to return (default 200)"},
                "offset": {"type": "integer", "description": "lines to skip from the start (default 0)"},
            },
            "required": ["file_path"],
        },
    },
    handler=_read,
    display=_disp_read,
)

WRITE_TOOL = Tool(
    schema={
        "name": "write",
        "description": (
            "Create or overwrite a utf-8 text file. "
            "An EXISTING file must have been read this session before it can be overwritten. "
            "Parent directories are created automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string", "description": "full file content"},
            },
            "required": ["file_path", "content"],
        },
    },
    handler=_write,
    display=_disp_write,
)

EDIT_TOOL = Tool(
    schema={
        "name": "edit",
        "description": (
            "Replace exactly one occurrence of old_string with new_string in a file. "
            "old_string must be UNIQUE in the file - include surrounding lines if it appears "
            "multiple times. Read the file before editing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string", "description": "exact text to replace, whitespace included"},
                "new_string": {"type": "string"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    handler=_edit,
    display=_disp_edit,
)
