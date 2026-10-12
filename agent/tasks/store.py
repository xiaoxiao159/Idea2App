"""task 存储:任务图持久化到 .tasks/{id}.json —— 多 agent 协作的地基。

每个任务一个 JSON 文件(subject/status/owner/blockedBy/worktree),依赖是一条 DAG:
- create / update_dependencies:建节点、加边(先全量建点,再用返回的 ID 加边)
- claim / complete:pending → in_progress → completed,认领防撞靠 owner

1. 原子性:`task_store_lock()` = 进程内 RLock + 跨进程文件锁(.tasks/.lock),
   认领/完成从「检查再写」的竞态窗口里抽出来 —— 两个 agent 同时 claim 同一任务,
   只会有一个成功。
2. 单任务认领:每个 owner(包括领导 "agent" 和命名队友)同一时间只能持有一个认领,
   认领登记成 teammate_assignments[owner]={"task_id","cwd"};完成不即释放,由回合边界
   release_completed_assignment 归还 cwd 租约。worktree 字段(isolation 目录)的 git
   逻辑在 team.py,这里只存名字并经由 worktree_cwd_resolver 反查真实目录。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from agent import locks

TASK_ID_PATTERN = re.compile(r"^task_[0-9a-f]{8}$")

TASKS_DIR = Path.cwd() / ".tasks"

teammate_assignments: dict[str, dict[str, object]] = {}#{"alice": {"task_id": "task_abc12345", "cwd": Path("/.../worktree-a")}}
assignment_versions: dict[str, int] = {}

# ---- team.py 注入(避免 store → team 循环)----
# worktree_cwd_resolver: Task -> (cwd, error)。工作树名→真实目录的解析在 team.py。
# plan_gate_get / plan_gate_reset: 计划闸门的读写也住在 team.py 的 plan_gates 字典里。
worktree_cwd_resolver = None
plan_gate_get = lambda owner: "not_required"  # noqa: E731
plan_gate_reset = lambda owner: None  # noqa: E731


def _task_cwd(task) -> tuple[Path, str | None]:
    """claim/complete,存进 teammate_assignments[owner]["cwd"]"""
    if worktree_cwd_resolver is not None:
        return worktree_cwd_resolver(task)
    return Path.cwd(), None

# 进程内可重入锁 + 线程本地深度,配合下面 task_store_lock 的跨进程文件锁
_task_rlock = threading.RLock()
_task_state = threading.local()


@contextmanager
def task_store_lock():
    """
    .tasks的锁
    进程内的RLock+跨进程的文件锁
    """
    with _task_rlock:#进入时，拿线程锁
        depth = getattr(_task_state, "depth", 0)
        outer = depth == 0
        if outer:
            TASKS.directory.mkdir(parents=True, exist_ok=True)
            file_lock = locks.exclusive_lock(TASKS.directory / ".lock")
            file_lock.__enter__()#拿文件锁
        _task_state.depth = depth + 1
        try:
            yield
        finally:
            _task_state.depth -= 1
            if outer:
                file_lock.__exit__(None, None, None)#放文件锁，退出时自动放线程锁


def advance_assignment_version(owner: str) -> None:
    """认领人换了工作单元:版本 +1,旧审批(plan / 别的)随之作废。"""
    with _task_rlock:
        assignment_versions[owner] = assignment_versions.get(owner, 0) + 1


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None    # 认领它的 agent;单 agent 阶段恒 "agent"
    blockedBy: list[str]  # 前置任务 id 列表
    worktree: str | None = None  # s13:可选的任务绑定工作树名


class TaskStore:
    """校验 ID、带路径逃逸防护的 JSON 读写。单条 CRUD,不是整表替换。"""

    def __init__(self, directory: Path):
        self.directory = directory

    def _root(self, create: bool = False) -> Path:
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        root = self.directory.resolve()
        if not root.is_relative_to(Path.cwd().resolve()):
            raise ValueError("task store escapes the workspace")
        return root

    def _path(self, task_id: str, create_root: bool = False) -> Path:
        if not isinstance(task_id, str) or not TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError(f"invalid task id: {task_id!r}")
        root = self._root(create=create_root)
        path = (root / f"{task_id}.json").resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"invalid task id: {task_id!r}")
        return path

    def exists(self, task_id: str) -> bool:
        return self._path(task_id).is_file()

    def create(self, subject: str, description: str = "") -> Task:
        subject = subject.strip()
        if not subject:
            raise ValueError("task subject cannot be empty")
        with task_store_lock():
            self._root(create=True)
            for _ in range(100):
                task = Task(
                    id=f"task_{secrets.token_hex(4)}",
                    subject=subject,
                    description=description,
                    status="pending",
                    owner=None,
                    blockedBy=[],
                )
                try:
                    with self._path(task.id, create_root=True).open(
                        "x", encoding="utf-8", newline="\n"
                    ) as handle:
                        json.dump(asdict(task), handle, indent=2, ensure_ascii=False)
                    return task
                except FileExistsError:
                    continue
            raise RuntimeError("could not allocate a unique task id")

    def _depends_on(self, task_id: str, target_id: str) -> bool:
        """task_id 是否(传递地)依赖 target_id —— 加边前的环检测。"""
        pending = [task_id]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(self.load(current).blockedBy)
        return False

    def update_dependencies(self, task_id: str, add_blocked_by: list[str]) -> Task:
        if not isinstance(add_blocked_by, list):
            raise ValueError("addBlockedBy must be a list of task ids")
        with task_store_lock():
            task = self.load(task_id)
            if task.status != "pending" or task.owner is not None:
                raise ValueError(
                    f"task {task_id} dependencies can only be updated while pending and unowned"
                )
            dependencies = list(dict.fromkeys(add_blocked_by))
            for dependency in dependencies:
                if dependency == task_id:
                    raise ValueError("task cannot depend on itself")
                if not self.exists(dependency):
                    raise ValueError(f"dependency not found: {dependency}")
                if dependency not in task.blockedBy and self._depends_on(dependency, task_id):
                    raise ValueError(f"dependency cycle detected: {task_id} -> {dependency}")
            task.blockedBy.extend(d for d in dependencies if d not in task.blockedBy)
            self.save(task)
            return task

    def save(self, task: Task) -> None:
        with task_store_lock():
            path = self._path(task.id, create_root=True)
            temporary = path.with_name(
                f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(asdict(task), indent=2, ensure_ascii=False), encoding="utf-8"
                )
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    def load(self, task_id: str) -> Task:
        with _task_rlock:
            data = json.loads(self._path(task_id).read_text(encoding="utf-8"))
            task = Task(**data)
            if task.id != task_id:
                raise ValueError(f"task file id does not match {task_id}")
            if task.status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"invalid task status: {task.status}")
            return task

    def list(self) -> list[Task]:
        if not self.directory.exists():
            return []
        root = self._root()
        return [self.load(p.stem) for p in sorted(root.glob("task_*.json"))]


TASKS = TaskStore(TASKS_DIR)


def incomplete_dependencies(task: Task) -> list[str]:
    """还没 completed 的前置(包括文件已消失的,一律当没完成)。"""
    incomplete: list[str] = []
    for dependency in task.blockedBy:
        try:
            if TASKS.load(dependency).status != "completed":
                incomplete.append(dependency)
        except (FileNotFoundError, ValueError, OSError):
            incomplete.append(dependency)
    return incomplete


def can_start(task_id: str) -> bool:
    return not incomplete_dependencies(TASKS.load(task_id))


def _owner_in_progress(owner: str) -> Task | None:
    return next(
        (t for t in TASKS.list() if t.status == "in_progress" and t.owner == owner),
        None,
    )


def claim(task_id: str, owner: str = "agent") -> tuple[str, bool]:
    """pending → in_progress(加锁原子),并把 owner 的 cwd 绑到任务工作树。"""
    with task_store_lock():
        task = TASKS.load(task_id)
        if task.status != "pending":
            return f"task {task_id} is {task.status}, cannot claim", True
        if task.owner:
            return f"task {task_id} is already owned by {task.owner}", True
        assignment = teammate_assignments.get(owner)
        if assignment:
            return (
                f"owner {owner} must finish the current work turn for "
                f"{assignment['task_id']} before claiming another task",
                True,
            )
        current = _owner_in_progress(owner)
        if current:
            return f"owner {owner} must complete {current.id} before claiming another", True
        blocked = incomplete_dependencies(task)
        if blocked:
            return f"task {task_id} is blocked by: {', '.join(blocked)}", True
        cwd, error = _task_cwd(task)
        if error:
            return f"cannot claim {task_id}: {error}", True
        task.owner = owner
        task.status = "in_progress"
        TASKS.save(task)
        teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
        advance_assignment_version(owner)
        return f"claimed {task.id} ({task.subject})", False


def complete(task_id: str, owner: str = "agent") -> tuple[str, bool]:
    """in_progress → completed,并显式报出刚被解锁的下游(加锁原子)。

    完成不即释放认领——cwd 租约保留到回合边界,由 release_completed_assignment 归还,
    这样同一回合里 lead 的文件/shell 工具仍落在该任务的工作树。
    """
    with task_store_lock():
        task = TASKS.load(task_id)
        if task.status != "in_progress":
            return f"task {task_id} is {task.status}, cannot complete", True
        if task.owner != owner:
            return f"task {task_id} is owned by {task.owner}, not {owner}", True
        gate = plan_gate_get(owner)
        if gate in {"required", "pending", "rejected"}:
            return f"task {task_id} cannot complete while plan status is {gate}", True
        ready_before = {
            t.id for t in TASKS.list()
            if t.status == "pending" and t.blockedBy and can_start(t.id)
        }
        assignment = teammate_assignments.get(owner)
        if not assignment or assignment.get("task_id") != task.id:
            cwd, error = _task_cwd(task)
            if error:
                return f"task {task_id} cannot complete: {error}", True
            teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
        task.status = "completed"
        TASKS.save(task)
        unblocked = [
            t.subject for t in TASKS.list()
            if t.status == "pending" and t.blockedBy
            and t.id not in ready_before and can_start(t.id)
        ]
        msg = f"completed {task.id} ({task.subject})"
        if unblocked:
            msg += f"\nunblocked: {', '.join(unblocked)}"
        return msg, False


def release_completed_assignment(owner: str) -> bool:
    """
        一个loop
        task completed后
    """
    with task_store_lock():
        assignment = teammate_assignments.get(owner)
        if not assignment:
            return False
        task = TASKS.load(str(assignment["task_id"]))
        if task.status != "completed" or task.owner != owner:
            return False
        teammate_assignments.pop(owner, None)
        advance_assignment_version(owner)
        plan_gate_reset(owner)
        return True


def release_teammate_assignment(owner: str) -> None:
    """
        teammates线程退出
        把没跑完的 in_progress 任务退回 pending,清空认领 + 复位闸门。"""
    with task_store_lock():
        try:
            task = _owner_in_progress(owner)
            if task:
                task.status = "pending"
                task.owner = None
                TASKS.save(task)
        finally:
            teammate_assignments.pop(owner, None)
            advance_assignment_version(owner)
            plan_gate_reset(owner)