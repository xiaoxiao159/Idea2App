"""workflow:s16 —— 把「编排」本身变成一次工具调用,计划即代码而非聊天回合。

一个 saved workflow 是一段可信脚本,里面用 ExecutionState 注入的原语编排子 agent:
- agent(prompt, schema)  : 跑一个子调用;带 schema 时强制结构化输出 + 校验(失败重试一次)
- parallel(thunks)       : 并发屏障,任一失败全失败
- pipeline(items, *stages): 逐条分阶段,阶段之间不设屏障(条 A 在 stage3 时条 B 还在 stage1)
- workflow(name, args)   : 内联跑一个 saved workflow(仅一层嵌套)
- phase(title) / log(msg): 进度事件

跑完落 `.runtime/<runId>.journal.jsonl`(append-only,resume 时按稳定哈希 key 复用已跑结果)
与 `.runtime/<runId>.json`(snapshot)。resume_from_run_id 校验原参数一致后断点续跑。

照参考实现 s16:runner 用 host 的同一个模型 client;这里走统一 Model 契约。
workflow 由 Workflow 工具同步触发(asyncio.run 桥接,参参考实现的 run_workflow_sync)。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path

from agent import locks
from agent.models.base import Model

AGENT_CAP = 1000             # 每个 run 的 agent() 调用硬上限
CONCURRENCY = 8              # 并发上限(信号量)
STORE = Path.cwd() / ".runtime"
MISS = object()              # journal 缓存未命中哨兵
WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_ID_RE = re.compile(r"^wf_[A-Za-z0-9][A-Za-z0-9._-]{0,63}_[0-9a-f]{16}$")


class WorkflowInputError(Exception):
    """workflow / meta / schema 输入不合法。"""


def _stable_hash(s: str) -> int:
    """跨进程稳定的哈希(Python 内建 hash 每进程盐化,会毁掉 resume key)。"""
    return int(hashlib.sha256(s.encode()).hexdigest(), 16)


def create_run_id(meta) -> str:
    return f"wf_{meta['name']}_{secrets.token_hex(8)}"


def reserve_run_id(meta, store: Path) -> str:
    """在 journal 被截断之前先占下一个 run 身份(独占文件 O_EXCL)。"""
    store.mkdir(parents=True, exist_ok=True)
    for _ in range(32):
        run_id = validate_run_id(create_run_id(meta))
        snapshot_path = store / f"{run_id}.json"
        try:
            fd = snapshot_path.open("xb")
        except FileExistsError:
            continue
        fd.close()
        return run_id
    raise WorkflowInputError("could not allocate a unique workflow runId")


def create_task_id(run_id: str) -> str:
    return f"local_workflow_{run_id}"


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise WorkflowInputError("invalid workflow runId")
    return run_id


_run_locks_guard = threading.Lock()
_run_locks: dict[str, threading.Lock] = {}


@contextlib.contextmanager
def _workflow_run_lock(run_id: str, store: Path):
    """跨线程 + 跨进程持锁整个 run 生命周期。已被占有 → WorkflowInputError。"""
    with _run_locks_guard:
        local_lock = _run_locks.setdefault(run_id, threading.Lock())
    if not local_lock.acquire(blocking=False):
        raise WorkflowInputError(f"workflow run {run_id} is already active")
    try:
        store.mkdir(parents=True, exist_ok=True)
        try:
            # 用本项目的跨平台文件锁替掉 fcntl
            with locks.exclusive_lock(store / f"{run_id}.lock", nonblocking=True):
                yield
        except OSError as exc:
            raise WorkflowInputError(f"workflow run {run_id} is already active") from exc
    finally:
        local_lock.release()
        with _run_locks_guard:
            if not local_lock.locked() and _run_locks.get(run_id) is local_lock:
                _run_locks.pop(run_id, None)


def validate_meta(meta) -> dict:
    """校验 name / description / 可选 phases,全部合法才放行。"""
    if not isinstance(meta, dict):
        raise WorkflowInputError("meta must be an object literal")
    if not meta.get("name") or not meta.get("description"):
        raise WorkflowInputError("meta requires `name` and `description`")
    if not isinstance(meta["name"], str) or not WORKFLOW_NAME_RE.fullmatch(meta["name"]):
        raise WorkflowInputError(
            "meta.name must be a 1-64 character slug using letters, numbers, '.', '_', or '-'"
        )
    if not isinstance(meta["description"], str):
        raise WorkflowInputError("meta.description must be a string")
    if "phases" in meta:
        if not isinstance(meta["phases"], list) or not all(
            isinstance(phase, str) and phase for phase in meta["phases"]
        ):
            raise WorkflowInputError("meta.phases must be a list of non-empty strings")
    return meta


def check_permission(meta, settings=None) -> str:
    settings = settings or {}
    if meta["name"] in settings.get("deny", []):
        raise WorkflowInputError(f"workflow '{meta['name']}' denied by settings")
    return "allow"


# -- 极简 JSON Schema 校验(agent({schema}) 的背后)----


class SimpleJsonSchema:
    """object/array/string/boolean/number + required keys。"""

    def __init__(self, schema) -> None:
        self.schema = schema

    def validate(self, value, schema=None):
        schema = self.schema if schema is None else schema
        if "enum" in schema and value not in schema["enum"]:
            return False, f"expected one of {schema['enum']}"
        t = schema.get("type")
        if t == "object":
            if not isinstance(value, dict):
                return False, "expected object"
            for key in schema.get("required", []):
                if key not in value:
                    return False, f"missing required key '{key}'"
            for key, sub in schema.get("properties", {}).items():
                if key in value:
                    ok, err = self.validate(value[key], sub)
                    if not ok:
                        return False, f"{key}: {err}"
            return True, None
        if t == "array":
            if not isinstance(value, list):
                return False, "expected array"
            items = schema.get("items")
            if items:
                for i, el in enumerate(value):
                    ok, err = self.validate(el, items)
                    if not ok:
                        return False, f"[{i}]: {err}"
            return True, None
        if t == "string":
            return (isinstance(value, str), None if isinstance(value, str) else "expected string")
        if t == "boolean":
            return (isinstance(value, bool), None if isinstance(value, bool) else "expected boolean")
        if t in ("number", "integer"):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            return (ok, None if ok else "expected number")
        return True, None


def _fill_schema(schema, seed):
    t = schema.get("type")
    if t == "object":
        keys = schema.get("required") or list(schema.get("properties", {}))
        return {k: _fill_schema(schema["properties"][k], f"{seed}/{k}") for k in keys}
    if t == "array":
        return [_fill_schema(schema["items"], f"{seed}/0")]
    if t == "boolean":
        return _stable_hash(seed) % 4 != 0
    if t in ("number", "integer"):
        return _stable_hash(seed) % 5
    return seed.rsplit("/", 1)[-1]


# -- Agent runner(可换成真实模型)----


@dataclass(frozen=True)
class RunnerOutput:
    value: object
    tokens: int


class MockAgentRunner:
    """确定性 runner,demo / 单测用,不碰网络。"""

    def run(self, prompt, schema=None, label=None):
        if schema is None:
            value = f"[mock] {(label or prompt)[:60]}"
            return RunnerOutput(value, self._tokens(prompt, value))
        props = schema.get("properties", {})
        if "findings" in props:
            n = 1 + (_stable_hash(prompt) % 2)
            sev = ["high", "medium", "low"]
            value = {"findings": [
                {"title": f"{label or 'audit'} #{i + 1}",
                 "severity": sev[_stable_hash(prompt + str(i)) % 3]}
                for i in range(n)
            ]}
        elif "isReal" in props:
            real = _stable_hash(prompt) % 4 != 0
            value = {"isReal": real, "reason": "reproduced" if real else "could not reproduce"}
        else:
            value = _fill_schema(schema, prompt)
        return RunnerOutput(value, self._tokens(prompt, value))

    @staticmethod
    def _tokens(prompt, result):
        return len(prompt) // 4 + len(json.dumps(result, default=str)) // 4


def _response_text(content) -> str:
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def _parse_runner_json(text: str) -> object:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for position, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(stripped[position:])
            except json.JSONDecodeError:
                continue
            return value
        raise WorkflowInputError("workflow agent returned invalid JSON")


WORKFLOW_AGENT_SYSTEM = (
    "You are a focused workflow agent. Complete only the supplied step. "
    "Do not claim access to files or results not included in the prompt."
)


class ModelAgentRunner:
    """走统一 Model 契约的 workflow agent runner(与 host 同模型)。"""

    def __init__(self, model: Model, max_tokens: int = 2000) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def run(self, prompt, schema=None, label=None):
        request = prompt
        if schema is not None:
            request += (
                "\n\nReturn only one JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=True, sort_keys=True)
            )
        response = self.model.complete(
            [{"role": "user", "content": request}], WORKFLOW_AGENT_SYSTEM, [], self.max_tokens
        )
        text = _response_text(response.get("content") or [])
        if schema is None:
            value = text
        else:
            try:
                value = _parse_runner_json(text)
            except WorkflowInputError:
                value = text  # 让 ExecutionState 的 schema 检查触发唯一一次重试
        usage = response.get("usage", {})
        tokens = int(usage.get("input", 0)) + int(usage.get("output", 0))
        return RunnerOutput(value, tokens)


RUNNER_FACTORY = MockAgentRunner


# -- Journal(resume 缓存)----


class WorkflowJournal:
    """append-only <runId>.journal.jsonl。resume 时 agent() 的语义 key 已存在则回放缓存。"""

    def __init__(self, run_id, resume, store: Path | None = None):
        store = STORE if store is None else store
        store.mkdir(parents=True, exist_ok=True)
        self.path = store / f"{run_id}.journal.jsonl"
        self.resume = resume
        self.cache = {}
        if resume:
            if not self.path.exists():
                raise WorkflowInputError(f"resume journal not found for {run_id}")
            for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
                try:
                    rec = json.loads(line)
                    if not isinstance(rec, dict) or not isinstance(rec.get("key"), str) or "value" not in rec:
                        raise ValueError("expected key/value record")
                except (json.JSONDecodeError, ValueError) as exc:
                    raise WorkflowInputError(
                        f"invalid resume journal record at line {line_number}"
                    ) from exc
                self.cache[rec["key"]] = rec["value"]
            self._f = self.path.open("a", encoding="utf-8")
        else:
            self._f = self.path.open("w", encoding="utf-8")  # 新 run 截断

    def key(self, kind, label, prompt, schema):
        basis = f"{kind}|{label}|{prompt}|{json.dumps(schema, sort_keys=True)}"
        return f"{kind}-{_stable_hash(basis) % 10**10:010d}"

    def cached(self, key):
        return self.cache.get(key, MISS)

    def record(self, key, value):
        self._f.write(json.dumps({"key": key, "value": value}) + "\n")
        self._f.flush()
        self.cache[key] = value

    def close(self):
        self._f.close()


class Budget:
    def __init__(self, total=None) -> None:
        self.total = total
        self._spent = 0

    def add(self, n):
        if self.total is not None and self._spent + n > self.total:
            raise WorkflowInputError(f"token budget exceeded ({self._spent + n} > {self.total})")
        self._spent += n

    def spent(self):
        return self._spent

    def remaining(self):
        return float("inf") if self.total is None else max(0, self.total - self._spent)


# -- Workflow task 生命周期 ----

class LocalWorkflowTask:
    def __init__(self, task_id, run_id, meta) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.meta = meta
        self.status = "running"
        self.usage = {"agents": 0, "tokens": 0}
        self.progress = []

    def event(self, name, **data):
        line = " ".join(f"{k}={v}" for k, v in data.items())
        print(f"  event      {name:<18} {line}")

    def progress_event(self, ptype, **data):
        self.progress.append({"type": ptype, **data})
        line = " ".join(f"{k}={v}" for k, v in data.items())
        print(f"  progress   {ptype:<16} {line}")


class ExecutionLimits:
    def __init__(self) -> None:
        self.agents = 0
        self.semaphore = asyncio.Semaphore(CONCURRENCY)

    def claim_agent(self):
        self.agents += 1
        if self.agents > AGENT_CAP:
            raise WorkflowInputError(f"agent() cap reached ({AGENT_CAP})")


class ExecutionState:
    """注入进 workflow 脚本的编排原语。"""

    def __init__(self, task, journal, runner, budget, args, depth=0, limits=None) -> None:
        self.task = task
        self.journal = journal
        self.runner = runner
        self.budget = budget
        self.args = args
        self._depth = depth
        self._phase = None
        self._phases_seen = set()
        self._limits = limits or ExecutionLimits()

    def phase(self, title):
        self._phase = title
        if title not in self._phases_seen:
            self._phases_seen.add(title)
            self.task.progress_event("workflow_phase", title=title)

    def log(self, message):
        self.task.progress_event("workflow_log", message=message)

    async def agent(self, prompt, schema=None, label=None, phase=None):
        label = label or (prompt[:24] + "...")
        self._limits.claim_agent()
        if self.budget.remaining() <= 0:
            raise WorkflowInputError("token budget exceeded")

        key = self.journal.key("agent", label, prompt, schema)
        cached = self.journal.cached(key)
        if cached is not MISS:
            if schema is not None:
                ok, err = SimpleJsonSchema(schema).validate(cached)
                if not ok:
                    raise WorkflowInputError(f"cached agent output failed schema validation: {err}")
            self.task.progress_event("workflow_agent", label=label,
                                     phase=phase or self._phase, status="cached")
            return cached

        async with self._limits.semaphore:
            run = await asyncio.to_thread(self.runner.run, prompt, schema, label)
            result, tokens = run.value, run.tokens

        if schema is not None:
            ok, err = SimpleJsonSchema(schema).validate(result)
            if not ok:
                retry = await asyncio.to_thread(
                    self.runner.run, prompt + "\n\nReturn valid JSON.", schema, label
                )
                result, tokens = retry.value, tokens + retry.tokens
                ok, err = SimpleJsonSchema(schema).validate(result)
                if not ok:
                    raise WorkflowInputError(f"agent({{schema}}) invalid output: {err}")

        self.budget.add(tokens)
        self.task.usage["agents"] += 1
        self.task.usage["tokens"] += tokens
        self.journal.record(key, result)
        self.task.progress_event("workflow_agent", label=label,
                                 phase=phase or self._phase, status="done")
        return result

    async def parallel(self, thunks):
        return await asyncio.gather(*[thunk() for thunk in thunks])

    async def pipeline(self, items, *stages):
        async def run_item(item, idx):
            value = item
            for stage in stages:
                value = await stage(value, item, idx)
            return value
        return await asyncio.gather(*[run_item(it, i) for i, it in enumerate(items)])

    async def workflow(self, name, args=None):
        if self._depth >= 1:
            raise WorkflowInputError("workflow() nesting is one level only")
        if name not in WORKFLOWS:
            raise WorkflowInputError(f"unknown workflow '{name}'")
        meta, fn = WORKFLOWS[name]
        child = ExecutionState(self.task, self.journal, self.runner, self.budget,
                               args or {}, depth=self._depth + 1, limits=self._limits)
        return await fn(child, args or {})


# -- Workflow tool ----

def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _save_last_run(run_id: str, store: Path = STORE) -> None:
    """把最近一次 run 写进 <store>/last_run.txt,给 CLI resume 当指针(见参考 s16)。"""
    (store / "last_run.txt").write_text(run_id, encoding="utf-8")


def _read_last_run(store: Path = STORE) -> str | None:
    p = store / "last_run.txt"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def _read_snapshot(run_id: str, store: Path):
    path = store / f"{run_id}.json"
    if not path.exists():
        raise WorkflowInputError(f"resume snapshot not found for {run_id}")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowInputError(f"invalid resume snapshot for {run_id}") from exc
    if not isinstance(snapshot, dict):
        raise WorkflowInputError(f"invalid resume snapshot for {run_id}")
    return snapshot


def serialize_task(task) -> dict:
    return {
        "taskId": task.task_id,
        "taskType": "local_workflow",
        "runId": task.run_id,
        "workflowName": task.meta["name"],
        "status": task.status,
        "usage": dict(task.usage),
        "progress": list(task.progress),
    }


class WorkflowTool:
    def __init__(self, store: Path | None = None) -> None:
        self.store = STORE if store is None else store

    async def call(self, meta, script_fn, args=None, resume_from_run_id=None):
        validate_meta(meta)
        check_permission(meta)
        resuming = resume_from_run_id is not None
        run_id = validate_run_id(resume_from_run_id) if resuming else reserve_run_id(meta, self.store)

        with _workflow_run_lock(run_id, self.store):
            return await self._call_locked(meta, script_fn, args, run_id, resuming)

    async def _call_locked(self, meta, script_fn, args, run_id, resuming):
        if resuming:
            snapshot = _read_snapshot(run_id, self.store)
            if snapshot.get("workflowName") != meta["name"]:
                raise WorkflowInputError("resume runId does not match workflow meta")
            saved_args = snapshot.get("args", {})
            if args is None:
                args = saved_args
            elif args != saved_args:
                raise WorkflowInputError("resume args do not match the original run")
            journal = WorkflowJournal(run_id, resume=True, store=self.store)
        else:
            args = args or {}
            journal = WorkflowJournal(run_id, resume=False, store=self.store)

        task_id = create_task_id(run_id)
        task = LocalWorkflowTask(task_id, run_id, meta)
        launched = {"status": "async_launched", "taskId": task_id,
                    "taskType": "local_workflow", "runId": run_id,
                    "workflowName": meta["name"]}
        task.event("async_launched", runId=run_id, taskId=task_id)
        task.event("task_started", workflow=meta["name"], taskId=task_id)
        _write_json(self.store / f"{run_id}.json", {
            "runId": run_id, "workflowName": meta["name"],
            "args": args, "task": serialize_task(task),
        })

        try:
            ctx = ExecutionState(task, journal, RUNNER_FACTORY(), Budget(args.get("budget")), args)
            result = await script_fn(ctx, args)
            task.status = "completed"
        except Exception as e:
            task.status = "failed"
            result = {"error": str(e)}
        finally:
            journal.close()

        _write_json(self.store / f"{run_id}.output.json", result)
        _write_json(self.store / f"{run_id}.json", {
            "runId": run_id, "workflowName": meta["name"],
            "args": args, "task": serialize_task(task),
        })
        _save_last_run(run_id, self.store)
        task.event("task_notification", status=task.status,
                   agents=task.usage["agents"], tokens=task.usage["tokens"],
                   outputFile=f".runtime/{run_id}.output.json")
        return {"launched": launched, "result": result, "task": task}


# -- sample workflow:review-changes(可信脚本,计划即代码)----

FINDINGS_SCHEMA = {
    "type": "object", "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object", "required": ["title", "severity"],
        "properties": {
            "title": {"type": "string"},
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        }}}},
}
VERDICT_SCHEMA = {
    "type": "object", "required": ["isReal", "reason"],
    "properties": {"isReal": {"type": "boolean"}, "reason": {"type": "string"}},
}

SAMPLE_META = {
    "name": "review-changes",
    "description": "Review changed files across dimensions, verify each finding",
    "phases": ["Review", "Verify"],
}

DIMENSIONS = ["correctness", "security", "performance", "style"]
DEMO_CHANGES = (
    "def load_user(user_id):\n"
    "    query = f\"SELECT * FROM users WHERE id = {user_id}\"\n"
    "    return db.execute(query).fetchone()\n"
)


async def sample_workflow(ctx, args):
    ctx.phase("Review")
    changes = args.get("changes", "")
    if not isinstance(changes, str):
        raise WorkflowInputError("args.changes must be a string")
    review_input = changes.strip() or "No change context was supplied."

    async def audit(_value, dimension, _idx):
        out = await ctx.agent(
            f"Review this change context for {dimension} issues. "
            "Report only issues supported by the supplied text.\n\n"
            f"{review_input}",
            schema=FINDINGS_SCHEMA, label=f"audit:{dimension}", phase="Review")
        return {"dimension": dimension, "findings": out["findings"]}

    async def verify(audited, dimension, _idx):
        ctx.phase("Verify")
        verdicts = await ctx.parallel([
            (lambda f=f: ctx.agent(
                f"Adversarially verify this {dimension} finding against the "
                "supplied change context.\n\n"
                f"Change context:\n{review_input}\n\n"
                f"Finding:\n{json.dumps(f, ensure_ascii=True)}",
                schema=VERDICT_SCHEMA, label=f"verify:{dimension}:{f['title']}", phase="Verify"))
            for f in audited["findings"]])
        confirmed = [f for f, v in zip(audited["findings"], verdicts) if v and v.get("isReal")]
        return {"dimension": dimension, "confirmed": confirmed}

    results = await ctx.pipeline(DIMENSIONS, audit, verify)
    confirmed = [{"dimension": r["dimension"], **f}
                 for r in results if r for f in r["confirmed"]]
    confirmed.sort(key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f["severity"], 3))
    ctx.log(f"confirmed {len(confirmed)} real finding(s)")
    return {"confirmed": confirmed}


WORKFLOWS = {SAMPLE_META["name"]: (SAMPLE_META, sample_workflow)}


async def run_workflow(name, args=None, resume_from_run_id=None, store=None):
    if not isinstance(name, str):
        raise WorkflowInputError("workflow name must be a string")
    if name not in WORKFLOWS:
        raise WorkflowInputError(f"unknown workflow '{name}'")
    if args is not None and not isinstance(args, dict):
        raise WorkflowInputError("workflow args must be an object")
    meta, script_fn = WORKFLOWS[name]
    out = await WorkflowTool(store).call(meta, script_fn, args=args,
                                         resume_from_run_id=resume_from_run_id)
    return {
        "launched": out["launched"],
        "result": out["result"],
        "task": serialize_task(out["task"]),
    }


def run_workflow_sync(name, args=None, resume_from_run_id=None, store=None):
    """同步桥:host 的同步 tool handler → async workflow runtime。"""
    try:
        return json.dumps(asyncio.run(run_workflow(name, args, resume_from_run_id, store)), default=str, ensure_ascii=False)
    except WorkflowInputError as exc:
        return f"Error: {exc}"


def set_runner(model: Model, max_tokens: int = 2000) -> None:
    """main 启动:把 RUNNER_FACTORY 换成走真实模型的 runner。"""
    global RUNNER_FACTORY
    RUNNER_FACTORY = lambda: ModelAgentRunner(model, max_tokens)