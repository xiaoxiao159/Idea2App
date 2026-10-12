"""会话持久化:每条往返 append-only 落盘成 JSONL,进程死亡也能续上、也能找回。

单机版不建索引库:目录名(时间戳)天然可排序,每会话一个 meta.json 记标题/用量/是否已关。
events.jsonl 每行一条,形状 = context.emit() 投影出的纯 dict —— 恢复就是逐行回放进窗口。

只记"新追加"的原始往返。窗口压不压、摘要存不存,是 context/compact 的事,
这份原始流是源头真相,概不掺合。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Session:
    id: str
    dir: Path
    events: Path
    meta: Path


CURRENT: Session | None = None  # 当前活跃会话;会话状态住这(旧的进程死即亡的活法,只此一个)


def begin(root: Path) -> Session:
    """新会话:建目录 + 空 meta。同秒碰撞加后缀,绝不覆盖旧会话。"""
    global CURRENT
    root.mkdir(parents=True, exist_ok=True)
    sid = _id()
    k = 2
    while (root / sid).exists():
        sid = f"{_id()}-{k}"
        k += 1
    d = root / sid
    d.mkdir(parents=True, exist_ok=True)
    s = Session(id=sid, dir=d, events=d / "events.jsonl", meta=d / "meta.json")
    _write(s, {
        "id": sid,
        "created": datetime.now().isoformat(timespec="seconds"),
        "title": "",
        "closed": False,
        "input": 0,
        "output": 0,
    })
    CURRENT = s
    return s


def attach(s: Session) -> None:
    """续旧会话:把 CURRENT 指上去,后续 record 继续追加到它的文件(重开)。"""
    global CURRENT
    CURRENT = s
    m = _read(s)
    m["closed"] = False
    _write(s, m)


def record(msg: dict[str, Any]) -> None:
    """追加一条往返。没 begin/attach 就 no-op —— 测试直接调 loop 不受影响。"""
    if CURRENT is None:
        return
    line = json.dumps(msg, ensure_ascii=False, default=str)#中文落盘
    with CURRENT.events.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
    # 第一条 user 原话当标题(只设一次,续旧会话不覆盖)
    if msg.get("role") == "user" and isinstance(msg.get("content"), str):
        m = _read(CURRENT)
        if not m.get("title"):
            m["title"] = msg["content"].strip().replace("\n", " ")[:60]
            _write(CURRENT, m)


def finish(summary: str | None = None, usage: dict[str, int] | None = None) -> None:
    """关会话:写入用量与可选摘要,钉上 closed。之后 record 变 no-op。"""
    global CURRENT
    if CURRENT is None:
        return
    m = _read(CURRENT)
    if usage:
        m["input"] = m.get("input", 0) + usage.get("input", 0)
        m["output"] = m.get("output", 0) + usage.get("output", 0)
    if summary:
        m["summary"] = summary
        (CURRENT.dir / "summary.md").write_text(summary, encoding="utf-8")
    m["closed"] = True
    _write(CURRENT, m)
    CURRENT = None


def list_sessions(root: Path) -> list[Session]:
    """扫目录,按 id(时间戳)倒序 = 新的在前。这就是单机的'索引'。"""
    if not root.is_dir():
        return []
    out: list[Session] = []
    for d in sorted(root.iterdir(), reverse=True):
        if d.is_dir():
            out.append(Session(id=d.name, dir=d, events=d / "events.jsonl", meta=d / "meta.json"))
    return out


def read_messages(s: Session) -> list[dict[str, Any]]:
    """读回原始往返(逐行 JSON)。坏行跳过 —— 退出瞬间被打断的半行不配炸掉整个恢复。"""
    if not s.events.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in s.events.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def count_messages(s: Session) -> int:
    """数往返条数,给选单显示用。不解析 JSON,只数非空行 —— 列表要快。
    (坏行也算进来,显示用途,不追求精确。)
    """
    if not s.events.is_file():
        return 0
    return sum(1 for ln in s.events.read_text(encoding="utf-8").splitlines() if ln.strip())


def read_meta(s: Session) -> dict[str, Any]:
    return _read(s)


def _id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _read(s: Session) -> dict[str, Any]:
    try:
        return json.loads(s.meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write(s: Session, meta: dict[str, Any]) -> None:
    s.meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")