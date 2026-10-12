"""team:s13 —— 常驻队友:共享任务 + 文件信箱 + 计划闸门 + 会话级协议。

    +------+  spawn(task_id)  +----------+  result  +------+
    | Lead | ---------------> |  WORK    | -------> | IDLE |
    +--+---+                  +----+-----+          +--+---+
       ^                           |                   |
       | team events               | tools             | wait
       |                           v                   v
    +--+-----------+          +----------+        +----------+
    | MessageBus   |          | Task cwd | <----- | 信箱     |
    +--------------+          +----------+  claim +----------+

    .tasks/     共享任务记录与依赖(store.py)
    .mailboxes/ 消息、结果、协议响应(本模块 MessageBus)
    .worktrees/ 可选的任务绑定工作目录(git worktree,见下)

照参考实现 s13_agent_teams:
- 一个 Lead(主 REPL)spawn 多个常驻队友线程;队友各有独立 messages[]、WORK/IDLE 两相。
- 消息走文件信箱(MessageBus),队友给 lead 发 result / idle_notification 两个事件,
  配合 lead 侧的 consume_lead_inbox 在回合边界投递。
- 任务认领靠 store 的 task_store_lock(跨线程+跨进程)原子化;一个队友同一时间一个任务。
- plan 闸门:require_plan 的队友先 submit_plan 等 lead review_plan 批准,才许 bash/write/edit。
- shutdown 请求/响应是一等协议(带 request_id + 状态机),不是「发个消息就完」。
"""
from __future__ import annotations

import json
import re
import random
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent import workspace
from agent.models.base import Model
from agent.tasks import store

WORKDIR = Path.cwd()
MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_ROOT = MAILBOX_DIR.resolve()
WORKTREES_DIR = WORKDIR / ".worktrees"
WORKTREES_ROOT = WORKTREES_DIR.resolve()

VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESERVED_TEAMMATE_NAMES = {"lead", "agent"}
VALID_WORKTREE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 队友是非交互线程,不能弹 prompt;底线命令这里拦(与 permission.DENY_FLOOR 同源)。
TEAM_DENY_FLOOR = ("rm -rf /", "sudo ", "shutdown", "reboot", "mkfs", ":(){ :|:& };:")

# ---- 模型绑定(进程级单例;main 启动时 set_model)----
CURRENT_MODEL: Model | None = None
CURRENT_MAX_TOKENS = 8000


def set_model(model: Model, max_tokens: int = 8000) -> None:
    global CURRENT_MODEL, CURRENT_MAX_TOKENS
    CURRENT_MODEL = model
    CURRENT_MAX_TOKENS = max_tokens


# ---- MessageBus:文件信箱,破坏性读 ----

def is_valid_agent_name(name: str) -> bool:
    return bool(VALID_AGENT_NAME.fullmatch(name))


class MessageBus:
    """线程安全的文件信箱。read 即删(破坏性读),Condition 支撑 wait_for_messages。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)

    def _path(self, agent: str) -> Path:
        if not is_valid_agent_name(agent):
            raise ValueError(f"Invalid mailbox recipient: {agent!r}")
        path = (MAILBOX_DIR / f"{agent}.jsonl").resolve()
        if not path.is_relative_to(MAILBOX_ROOT):
            raise ValueError(f"Mailbox path escapes directory: {agent!r}")
        return path

    def _read_unlocked(self, agent: str) -> list[dict]:
        inbox = self._path(agent)
        if not inbox.exists():
            return []
        msgs = [
            json.loads(line)
            for line in inbox.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        inbox.unlink()
        return msgs

    def send(self, from_agent, to_agent, content, msg_type="message", metadata=None):
        msg = {"from": from_agent, "to": to_agent, "content": content,
               "type": msg_type, "ts": time.time(), "metadata": metadata or {}}
        with self._changed:
            MAILBOX_DIR.mkdir(parents=True, exist_ok=True)
            with self._path(to_agent).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(msg, ensure_ascii=True) + "\n")
            self._changed.notify_all()
        print(f"  [bus] {from_agent} -> {to_agent}: ({msg_type}) {content[:50]}")

    def read_inbox(self, agent: str) -> list[dict]:
        with self._lock:
            return self._read_unlocked(agent)

    def peek(self, agent: str) -> bool:
        with self._lock:
            inbox = self._path(agent)
            return inbox.exists() and inbox.stat().st_size > 0

    def wait_for_messages(self, agent: str, timeout=None) -> list[dict]:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(agent):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return []
                self._changed.wait(remaining)
            return self._read_unlocked(agent)


BUS = MessageBus()

# working | waiting_approval | idle | stopping
active_teammates: dict[str, str] = {}
plan_gates: dict[str, str] = {}
plan_request_ids: dict[str, str] = {}
teammate_threads: dict[str, threading.Thread] = {}
team_lock = threading.RLock()


@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    work_version: int | None = None
    task_id: str | None = None
    created_at: float = field(default_factory=time.time)


pending_requests: dict[str, ProtocolState] = {}


def new_request_id() -> str:
    while True:
        request_id = f"req_{random.randint(0, 999999):06d}"
        if request_id not in pending_requests:
            return request_id


def match_response(response_type, request_id, approve, from_agent, to_agent) -> bool:
    with team_lock:
        state = pending_requests.get(request_id)
        if not state:
            print(f"  [protocol] unknown request_id: {request_id}")
            return False
        expected = {
            "shutdown": "shutdown_response",
            "plan_approval": "plan_approval_response",
        }[state.type]
        if response_type != expected:
            print(f"  [protocol] expected {expected}, got {response_type}")
            return False
        if from_agent != state.target or to_agent != state.sender:
            print(f"  [protocol] {request_id} responder mismatch")
            return False
        if state.status != "pending":
            print(f"  [protocol] {request_id} already {state.status}")
            return False
        state.status = "approved" if approve else "rejected"
    print(f"  [protocol] {request_id} -> {state.status}")
    return True


def consume_lead_inbox() -> list[dict]:
    """消费 Lead 信箱,顺手把 *_response 协议状态更新掉,再交给 main 投递。"""
    msgs = BUS.read_inbox("lead")
    for msg in msgs:
        metadata = msg.get("metadata", {})
        request_id = metadata.get("request_id", "")
        if request_id and msg.get("type", "").endswith("_response"):
            match_response(msg["type"], request_id, metadata.get("approve", False),
                           msg.get("from", ""), msg.get("to", ""))
    return msgs


def format_team_events(msgs: list[dict]) -> str:
    lines = []
    for msg in msgs:
        metadata = msg.get("metadata", {})
        request_id = metadata.get("request_id")
        suffix = f" request_id={request_id}" if request_id else ""
        lines.append(f"[{msg['type']}{suffix}] {msg['from']}: {msg['content']}")
    return "[Team events]\n" + "\n".join(lines)


def has_lead_events() -> bool:
    return BUS.peek("lead")


def _last_text(content) -> str:
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", "")).strip()
    return ""


# ---- worktree:git worktree 绑定(非 git 仓库时 fail closed)----

def validate_worktree_name(name: str) -> str | None:
    if not isinstance(name, str) or not VALID_WORKTREE_NAME.fullmatch(name):
        return ("worktree name must be 1-64 letters, digits, dots, underscores, "
                "or dashes, and start with a letter or digit")
    if name in {".", ".."} or ".." in name:
        return "worktree name cannot contain '..'"
    return None


def _worktree_path(name: str) -> Path:
    path = (WORKTREES_DIR / name).resolve()
    if (not WORKTREES_ROOT.is_relative_to(WORKDIR.resolve())
            or not path.is_relative_to(WORKTREES_ROOT)
            or path == WORKTREES_ROOT):
        raise ValueError(f"Worktree path escapes directory: {name!r}")
    return path


def _worktree_branch(name: str) -> str:
    return f"wt/{name}"


def _run_git(args, cwd=None):
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd or WORKDIR, capture_output=True,
            text=True, errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output or "(no output)"


def run_git(args, cwd=None):
    ok, output = _run_git(args, cwd)
    return ok, output[:5000]


def _registered_worktrees():
    ok, output = _run_git(["worktree", "list", "--porcelain"])
    if not ok:
        return {}, f"cannot read Git worktree registry: {output}"
    entries: dict[Path, dict] = {}
    current: dict = {}
    for line in output.splitlines() + [""]:
        if not line:
            raw_path = current.get("worktree")
            if raw_path:
                entries[Path(raw_path).resolve()] = current
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return entries, None


def _registered_worktree(name: str):
    try:
        path = _worktree_path(name)
    except ValueError as exc:
        return None, str(exc)
    entries, error = _registered_worktrees()
    if error:
        return None, error
    if path not in entries:
        return None, f"worktree '{name}' is not registered with Git"
    if not path.is_dir():
        return None, f"worktree '{name}' is missing at {path}"
    expected_branch = f"refs/heads/{_worktree_branch(name)}"
    if entries[path].get("branch") != expected_branch:
        return None, f"worktree '{name}' is not registered on expected branch"
    return path, None


def task_worktree_cwd(task) -> tuple[Path, str | None]:
    """解析一个任务的 cwd;worktree 绑定坏了就 fail closed。"""
    if not task.worktree:
        return WORKDIR, None
    path, error = _registered_worktree(task.worktree)
    return (path or WORKDIR), error


def teammate_cwd(owner: str) -> tuple[Path | None, str | None]:
    """队友的文件/shell 工具从这里拿当前任务绑定的 cwd。没认领任务 → 报错。"""
    assignment = store.teammate_assignments.get(owner)
    task = store._owner_in_progress(owner)
    if task:
        return task_worktree_cwd(task)
    if assignment:
        assigned = store.TASKS.load(str(assignment["task_id"]))
        if assigned.status in ("in_progress", "completed") and assigned.owner == owner:
            return task_worktree_cwd(assigned)
    return None, "Error: Claim a Task before using workspace tools."


def assignment_cwd(owner: str) -> Path:
    """owner 当前认领任务的真实工作目录。没认领 → 进程 cwd;坏认领 → raise。

    和参考 s15 的 assignment_cwd 同构:认领登记里带 cwd 租约,这里断言租约仍有效
    (任务还在 owner 名下、工作树目录没被挪走),再把目录交给文件/shell 工具。
    """
    with store.task_store_lock():
        assignment = store.teammate_assignments.get(owner)
        task = store._owner_in_progress(owner)
        if task and (not assignment or assignment.get("task_id") != task.id):
            cwd, error = task_worktree_cwd(task)
            if error:
                raise ValueError(error)
            assignment = {"task_id": task.id, "cwd": cwd}
            store.teammate_assignments[owner] = assignment
        elif not assignment:
            return WORKDIR
        task = store.TASKS.load(str(assignment["task_id"]))
        if task.status not in {"in_progress", "completed"} or task.owner != owner:
            raise ValueError(f"assignment for {owner} is no longer active")
        cwd, error = task_worktree_cwd(task)
        if error:
            raise ValueError(error)
        if cwd.resolve() != Path(assignment["cwd"]).resolve():
            raise ValueError(f"assignment cwd changed for task {task.id}")
        return cwd


def create_worktree(name: str, task_id: str) -> str:
    error = validate_worktree_name(name)
    if error:
        return f"Error: {error}"
    try:
        path = _worktree_path(name)
        task_path = store.TASKS._path(task_id)
    except ValueError as exc:
        return f"Error: {exc}"
    branch = _worktree_branch(name)

    with store.task_store_lock():
        if not task_path.exists():
            return f"Error: Task {task_id} not found"
        task = store.TASKS.load(task_id)
        if task.status != "pending" or task.owner is not None:
            return f"Error: Task {task_id} must be pending and unowned"
        if task.worktree:
            return f"Error: Task {task_id} already uses worktree '{task.worktree}'"
        if any(t.worktree == name for t in store.TASKS.list() if t.id != task_id):
            return f"Error: Worktree '{name}' is already bound to another task"
        if path.exists():
            return f"Error: Worktree path already exists: {path}"

        ok, root = run_git(["rev-parse", "--show-toplevel"])
        if not ok or Path(root).resolve() != WORKDIR.resolve():
            return "Error: Working directory must be the root of a Git repository"
        ok, branch_check = run_git(["check-ref-format", "--branch", branch])
        if not ok:
            return f"Error: Invalid worktree branch '{branch}': {branch_check}"
        exists, _ = run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
        if exists:
            return f"Error: Branch '{branch}' already exists"
        entries, registry_error = _registered_worktrees()
        if registry_error:
            return f"Error: {registry_error}"
        if path in entries:
            return f"Error: Worktree path is already registered: {path}"

        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        ok, result = run_git(["worktree", "add", "-b", branch, str(path), "HEAD"])
        if not ok:
            return (f"Partial operation: git worktree add reported an error "
                    f"after possibly leaving artifacts. Task {task_id} remains "
                    f"unbound and no Git data was deleted. Git error: {result}")

        try:
            task.worktree = name
            store.TASKS.save(task)
        except Exception as exc:
            return (f"Partial success: Worktree '{name}' was created at {path} "
                    f"on branch '{branch}', but task binding failed: {exc}.")

    print(f"  [worktree] created: {name} at {path}")
    return f"Worktree '{name}' created at {path} for task {task_id}"


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    error = validate_worktree_name(name)
    if error:
        return f"Error: {error}"
    with store.task_store_lock():
        path, error = _registered_worktree(name)
        if error:
            return f"Error: {error}"
        bound = [task for task in store.TASKS.list() if task.worktree == name]
        if not bound:
            return f"Error: Worktree '{name}' is not bound to a task"
        active = [task for task in bound if task.status != "completed"]
        if active:
            return (f"Error: Worktree '{name}' is bound to active task "
                    f"{active[0].id}; complete it before removal")
        leased = [owner for owner, assignment in store.teammate_assignments.items()
                  if assignment.get("cwd") and
                  Path(assignment["cwd"]).resolve() == path.resolve()]
        if leased:
            return f"Error: Worktree '{name}' is still in use by {', '.join(sorted(leased))}"

        args = ["worktree", "remove"]
        if discard_changes:
            args.append("--force")
        args.append(str(path))
        ok, result = run_git(args)
        if not ok:
            return f"Git error: {result}"
        try:
            for task in bound:
                task.worktree = None
                store.TASKS.save(task)
        except Exception as exc:
            return (f"Partial success: Worktree '{name}' was removed but task "
                    f"unbinding failed: {exc}.")
    print(f"  [worktree] removed: {name}; branch retained")
    return f"Worktree '{name}' removed; branch retained"


# ---- 队友的 cwd 感知基础工具(文件/shell)----

def _safe_path(p: str, cwd: Path) -> Path:
    base = cwd.resolve()
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _run_bash(command: str, cwd: Path) -> str:
    try:
        result = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True,
            text=True, errors="replace", timeout=120,
        )
        output = (result.stdout + result.stderr).strip()[:50000] or "(no output)"
        if result.returncode:
            return f"Error: command exited with status {result.returncode}\n{output}"
        return output
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except OSError as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def _run_read(path: str, cwd: Path, limit: int | None = None) -> str:
    try:
        lines = _safe_path(path, cwd).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _run_write(path: str, content: str, cwd: Path) -> str:
    try:
        target = _safe_path(path, cwd)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def _run_edit(path: str, old_text: str, new_text: str, cwd: Path) -> str:
    try:
        target = _safe_path(path, cwd)
        text = target.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            return f"Error: Expected 1 occurrence, found {count}"
        target.write_text(text.replace(old_text, new_text), encoding="utf-8")
        return f"Edited {path}"
    except Exception as exc:
        return f"Error: {exc}"


def _run_glob(pattern: str, cwd: Path) -> str:
    try:
        base = cwd.resolve()
        matches = [
            str(path.relative_to(base))
            for path in sorted(base.glob(pattern))
            if path.resolve().is_relative_to(base)
        ]
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) or "No files found"
    except Exception as exc:
        return f"Error: {exc}"


def _render_task_list() -> str:
    tasks = store.TASKS.list()
    if not tasks:
        return "No tasks. Use create_task to add some."
    marks = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    lines = [f"{marks.get(t.status, '[?]')} {t.id}: {t.subject} [{t.status}]" for t in tasks]
    return "\n".join(lines)


# ---- 协议提交 / 计划闸门 ----

def current_work_identity(owner: str):
    assignment = store.teammate_assignments.get(owner)
    task_id = assignment["task_id"] if assignment else None
    return store.assignment_versions.get(owner, 0), task_id


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    with team_lock:
        if plan_gates.get(from_name) == "pending":
            return "A plan is already waiting for review."
        assignment = store.teammate_assignments.get(from_name)
        task_id = assignment["task_id"] if assignment else None
        work_version = store.assignment_versions.get(from_name, 0)
        request_id = new_request_id()
        pending_requests[request_id] = ProtocolState(
            request_id=request_id, type="plan_approval", sender=from_name,
            target="lead", status="pending", payload=plan,
            work_version=work_version, task_id=task_id,
        )
        plan_gates[from_name] = "pending"
        plan_request_ids[from_name] = request_id
        active_teammates[from_name] = "waiting_approval"
    BUS.send(from_name, "lead", plan, "plan_approval_request", {"request_id": request_id})
    return f"Plan submitted ({request_id}). Wait for Lead's decision."


def _teammate_send_message(from_name: str, to: str, content: str) -> str:
    with team_lock:
        if to != "lead" and to not in active_teammates:
            return f"Agent '{to}' is not active"
    BUS.send(from_name, to, content)
    return f"Sent to {to}"


# ---- 队友工具定义 ----

TEAMMATE_TOOLS = [
    {"name": "bash", "description": "Run a shell command in the task's directory.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"file_path": {"type": "string"},
                                     "limit": {"type": "integer"}},
                      "required": ["file_path"]}},
    {"name": "write", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"file_path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["file_path", "content"]}},
    {"name": "edit", "description": "Replace exact text once.",
     "input_schema": {"type": "object",
                      "properties": {"file_path": {"type": "string"},
                                     "old_string": {"type": "string"},
                                     "new_string": {"type": "string"}},
                      "required": ["file_path", "old_string", "new_string"]}},
    {"name": "glob", "description": "Find files by glob pattern.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
    {"name": "send_message",
     "description": "Send an intermediate message to 'lead' or an active teammate.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "submit_plan", "description": "Submit a work plan for Lead approval.",
     "input_schema": {"type": "object",
                      "properties": {"plan": {"type": "string"}},
                      "required": ["plan"]}},
    {"name": "list_tasks", "description": "List shared tasks.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "claim_task", "description": "Claim a ready task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task", "description": "Complete an owned task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
]


def apply_plan_response(name: str, msg: dict):
    metadata = msg.get("metadata", {})
    request_id = metadata.get("request_id", "")
    work_version, task_id = current_work_identity(name)
    with team_lock:
        state = pending_requests.get(request_id)
        expected_id = plan_request_ids.get(name)
        valid = (
            msg.get("from") == "lead"
            and msg.get("to") == name
            and request_id == expected_id
            and state is not None
            and state.type == "plan_approval"
            and state.sender == name
            and state.target == "lead"
            and state.work_version == work_version
            and state.task_id == task_id
            and state.status in {"approved", "rejected"}
            and metadata.get("approve", False) == (state.status == "approved")
        )
        if not valid:
            return False, "[Ignored plan response: request mismatch]"
        plan_gates[name] = state.status
        active_teammates[name] = "working"
        plan_request_ids.pop(name, None)
        outcome = state.status
    return True, f"[Plan {outcome}] {msg['content']}"


def apply_shutdown_request(name: str, msg: dict):
    request_id = msg.get("metadata", {}).get("request_id", "")
    with team_lock:
        state = pending_requests.get(request_id)
        valid = (
            msg.get("from") == "lead"
            and msg.get("to") == name
            and state is not None
            and state.type == "shutdown"
            and state.sender == "lead"
            and state.target == name
            and state.status == "pending"
            and active_teammates.get(name) != "stopping"
        )
        if not valid:
            return False, "[Ignored shutdown request: request mismatch]"
        active_teammates[name] = "stopping"
    return True, request_id


# ---- 队友运行时 ----

IDLE_SCAN_INTERVAL = 2.0


class TeammateRuntime:
    """一个常驻队友:独立 messages[]、WORK/IDLE 两相、计划闸门阻塞文件改动。"""

    def __init__(self, name, role, prompt, task_id, require_plan):
        self.name = name
        self.system = (
            f"You are '{name}', a {role}. Use tools to complete the assigned Task, "
            "then call complete_task and report a concise result. If the first user "
            "message contains [Assigned task], that Task is already claimed; do not "
            "call claim_task for it again. When asked for a plan, call submit_plan and "
            "wait for approval before bash or file changes. File and shell tools use "
            "the Task's working directory; that directory is not a sandbox. The runtime "
            "delivers your final text to Lead. Use send_message only for intermediate "
            "coordination, and address the coordinator as 'lead'."
        )
        self.messages = [{"role": "user", "content": prompt}]
        if task_id:
            task = store.TASKS.load(task_id)
            self.messages[0]["content"] += (
                f"\n\n[Assigned task {task.id}] {task.subject}\n"
                f"{task.description}\nWork directory: {WORKDIR}"
            )
        if require_plan:
            self.messages[0]["content"] += (
                "\n\n[Plan required] Submit a plan and wait for Lead approval "
                "before changing files or using bash."
            )
        self.handlers = {
            "bash": self.bash,
            "read": self.read,
            "write": self.write,
            "edit": self.edit,
            "glob": self.glob,
            "send_message": lambda to, content: _teammate_send_message(name, to, content),
            "submit_plan": lambda plan: _teammate_submit_plan(name, plan),
            "list_tasks": _render_task_list,
            "claim_task": self.claim,
            "complete_task": self.complete,
        }

    def _cwd(self):
        cwd, error = teammate_cwd(self.name)
        return cwd or WORKDIR, error

    def bash(self, command):
        cwd, error = self._cwd()
        return error or _run_bash(command, cwd)

    def read(self, file_path, limit=None):
        cwd, error = self._cwd()
        return error or _run_read(file_path, cwd, limit)

    def write(self, file_path, content):
        cwd, error = self._cwd()
        return error or _run_write(file_path, content, cwd)

    def edit(self, file_path, old_string, new_string):
        cwd, error = self._cwd()
        return error or _run_edit(file_path, old_string, new_string, cwd)

    def glob(self, pattern):
        cwd, error = self._cwd()
        return error or _run_glob(pattern, cwd)

    def claim(self, task_id):
        return store.claim(task_id, owner=self.name)[0]

    def complete(self, task_id):
        return store.complete(task_id, owner=self.name)[0]

    def _run_tool(self, block):
        name = block["name"]
        params = block.get("input", {})
        gate = plan_gates.get(self.name, "not_required")
        if name in {"bash", "write", "edit"} and gate not in {"approved", "not_required"}:
            return (f"Blocked: plan status is {gate}. Submit or revise the plan and "
                    "wait for approval before changing the workspace.")
        if name == "bash":
            for bad in TEAM_DENY_FLOOR:
                if bad in str(params.get("command", "")):
                    return f"deny floor: command matched '{bad}'."
        handler = self.handlers.get(name)
        if not handler:
            return f"Unknown tool: {name}"
        try:
            return str(handler(**params))
        except Exception as exc:
            return f"Error: {type(exc).__name__}: {exc}"

    def handle_inbox(self, inbox):
        work_messages = []
        for msg in inbox:
            msg_type = msg.get("type", "message")
            if msg_type == "shutdown_request":
                accepted, notice = apply_shutdown_request(self.name, msg)
                if not accepted:
                    work_messages.append(notice)
                    continue
                BUS.send(self.name, "lead", "Shutdown acknowledged.",
                         "shutdown_response", {"request_id": notice, "approve": True})
                return True
            if msg_type == "plan_approval_response":
                _, notice = apply_plan_response(self.name, msg)
                work_messages.append(notice)
                continue
            if msg_type == "plan_request":
                work_messages.append(f"[Plan required] {msg['content']}")
                continue
            work_messages.append(f"[Message from {msg['from']}] {msg['content']}")
        if work_messages:
            self.messages.append({"role": "user", "content": "\n".join(work_messages)})
        return False

    def work(self):
        if self.handle_inbox(BUS.read_inbox(self.name)):
            return "stop"
        with team_lock:
            active_teammates[self.name] = "working"
        try:
            if CURRENT_MODEL is None:
                raise RuntimeError("teammate model not set")
            response = CURRENT_MODEL.complete(
                self.messages, self.system, TEAMMATE_TOOLS, CURRENT_MAX_TOKENS
            )
        except Exception as exc:
            BUS.send(self.name, "lead", f"{type(exc).__name__}: {exc}", "error")
            return "stop"

        content = response.get("content") or []
        self.messages.append({"role": "assistant", "content": content})
        tool_calls = [b for b in content if b.get("type") == "tool_use"]
        if tool_calls:
            results = []
            for block in tool_calls:
                output = self._run_tool(block)
                results.append({"type": "tool_result",
                                "tool_use_id": block["id"], "content": output})
            self.messages.append({"role": "user", "content": results})
            return "continue"

        summary = _last_text(content)
        gate = plan_gates.get(self.name, "not_required")
        if gate != "pending" and summary:
            BUS.send(self.name, "lead", summary, "result")
        if gate == "pending":
            with team_lock:
                active_teammates[self.name] = "waiting_approval"
        else:
            store.release_completed_assignment(self.name)
            with team_lock:
                active_teammates[self.name] = "idle"
            BUS.send(self.name, "lead", "Waiting for more work.", "idle_notification")
        return "idle"

    def wait_for_work(self):
        while True:
            inbox = BUS.wait_for_messages(self.name, IDLE_SCAN_INTERVAL)
            if inbox:
                before = len(self.messages)
                if self.handle_inbox(inbox):
                    return False
                if len(self.messages) > before:
                    return True
                continue
            task = self._claim_next()
            if task is None:
                continue
            self.messages.append({
                "role": "user",
                "content": f"[Auto-claimed task {task.id}] {task.subject}\nWork directory: {WORKDIR}",
            })
            print(f"  [idle] {self.name} claimed {task.id}: {task.subject}")
            return True

    def _claim_next(self):
        with store.task_store_lock():
            if store.teammate_assignments.get(self.name) or store._owner_in_progress(self.name):
                return None
        for task in store.TASKS.list():
            if task.status != "pending" or task.owner is not None or not store.can_start(task.id):
                continue
            result = store.claim(task.id, owner=self.name)[0]
            if result.startswith("claimed"):
                return store.TASKS.load(task.id)
        return None

    def run(self):
        try:
            state = "continue"
            while state != "stop":
                if state == "idle" and not self.wait_for_work():
                    break
                state = self.work()
        except Exception as exc:
            try:
                BUS.send(self.name, "lead", f"{type(exc).__name__}: {exc}", "error")
            except Exception:
                pass
        finally:
            try:
                store.release_teammate_assignment(self.name)
            except Exception as exc:
                try:
                    BUS.send(self.name, "lead",
                             f"Assignment cleanup failed: {type(exc).__name__}: {exc}", "error")
                except Exception:
                    pass
            with team_lock:
                active_teammates.pop(self.name, None)
                plan_gates.pop(self.name, None)
                plan_request_ids.pop(self.name, None)
                teammate_threads.pop(self.name, None)
            print(f"  [teammate] {self.name} finished")


def spawn_teammate_thread(name, role, prompt, task_id=None, require_plan=False) -> str:
    """认领初始任务(可选),起一个常驻队友线程。"""
    if not is_valid_agent_name(name):
        return "Invalid teammate name: use 1-64 letters, digits, underscores, or dashes"
    if name.lower() in RESERVED_TEAMMATE_NAMES:
        return f"Invalid teammate name: '{name}' is reserved by the runtime"
    with team_lock:
        if any(existing.casefold() == name.casefold() for existing in active_teammates):
            return f"Teammate '{name}' already exists"
        active_teammates[name] = "working"
        plan_gates[name] = "required" if require_plan else "not_required"
        store.assignment_versions[name] = 0

    if task_id:
        try:
            claimed = store.claim(task_id, owner=name)[0]
        except (FileNotFoundError, ValueError) as exc:
            claimed = f"Error: {exc}"
        if not claimed.startswith("claimed"):
            with team_lock:
                active_teammates.pop(name, None)
                plan_gates.pop(name, None)
            return f"Cannot spawn teammate '{name}': {claimed}"

    runtime = TeammateRuntime(name, role, prompt, task_id, require_plan)
    thread = threading.Thread(target=runtime.run, daemon=True)
    with team_lock:
        teammate_threads[name] = thread
    thread.start()
    print(f"  [teammate] {name} spawned as {role}")
    assigned = f" for {task_id}" if task_id else " without an initial Task"
    return (
        f"Teammate '{name}' spawned as {role}{assigned}. "
        "End this turn; the runtime will deliver its events."
    )


# ---- Lead 团队工具 handler(由 tools/team.py 包成 Tool)----


def run_spawn_teammate(name, role, prompt, task_id=None, require_plan=False) -> str:
    return spawn_teammate_thread(name, role, prompt, task_id, require_plan)


def run_list_teammates() -> str:
    with team_lock:
        if not active_teammates:
            return "No active teammates."
        return "\n".join(f"{name}: {status}"
                         for name, status in sorted(active_teammates.items()))


def run_send_message(to, content) -> str:
    if to not in active_teammates:
        return f"Teammate '{to}' is not active"
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def run_request_shutdown(teammate) -> str:
    if teammate not in active_teammates:
        return f"Teammate '{teammate}' is not active"
    with team_lock:
        request_id = new_request_id()
        pending_requests[request_id] = ProtocolState(
            request_id=request_id, type="shutdown", sender="lead",
            target=teammate, status="pending", payload="",
        )
    BUS.send("lead", teammate, "Finish the current step and shut down.",
             "shutdown_request", {"request_id": request_id})
    return f"Shutdown requested from {teammate} ({request_id})"


def run_request_plan(teammate, task) -> str:
    if teammate not in active_teammates:
        return f"Teammate '{teammate}' is not active"
    with team_lock:
        plan_gates[teammate] = "required"
    BUS.send("lead", teammate, task, "plan_request")
    return f"Plan requested from {teammate}"


def run_review_plan(request_id, approve, feedback="") -> str:
    with team_lock:
        state = pending_requests.get(request_id)
        if not state:
            return f"Request {request_id} not found"
        if state.type != "plan_approval":
            return f"Request {request_id} is not a plan"
        if state.status != "pending":
            return f"Request {request_id} already {state.status}"
        work_version, task_id = current_work_identity(state.sender)
        if state.work_version != work_version or state.task_id != task_id:
            return f"Request {request_id} belongs to an earlier assignment"
        if plan_request_ids.get(state.sender) != request_id:
            return f"Request {request_id} is not the current plan"
        state.status = "approved" if approve else "rejected"
    content = feedback or ("Plan approved." if approve else "Revise the plan and submit it again.")
    BUS.send("lead", state.sender, content, "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    return f"Plan {state.status} ({request_id})"


def run_create_worktree(name, task_id) -> str:
    return create_worktree(name, task_id)


# ---- 接线:把 store 需要的团队侧事实注入进去(避免 store → team 循环)----
# 任务工作树→真实目录的解析、计划闸门的读写,都住在本模块;store 只调钩子。
store.worktree_cwd_resolver = task_worktree_cwd
store.plan_gate_get = lambda owner: plan_gates.get(owner, "not_required")


def _reset_plan_gate(owner: str) -> None:
    if owner in plan_gates:
        plan_gates[owner] = "not_required"


store.plan_gate_reset = _reset_plan_gate


def _lead_cwd() -> Path:
    """lead 的工具 cwd:跟随当前认领任务的工作树;异常/未认领 → 退回进程 cwd。"""
    try:
        return assignment_cwd("agent")
    except (ValueError, FileNotFoundError):
        return WORKDIR


workspace.set_cwd_resolver(_lead_cwd)