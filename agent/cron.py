"""cron:s12 —— 把 prompt 挂到本地时间表上,到点由后台线程投递进主循环。

分工(照参考实现 s12_cron_scheduler):
- 5 字段 cron(minute hour day-of-month month day-of-week),支持 `*`、`*/N`、`N`、
  `N-M`、`N,M` 五种写法;`validate_cron` 逐字段验,`cron_matches` 判某时刻是否命中。
- 一个调度线程每 1s `poll_due_jobs()`:到期任务标记 pending_delivery 并入队;
  一个队列处理线程每 0.2s 抢 `agent_lock`,抢到就把队列里的 prompt 投进主循环。
- durable=True 的任务落盘 `.scheduled_tasks.json`(临时文件 + os.replace 原子写),
  进程重启后 `load_durable_jobs()` 找回;one-shot 任务投递后即删。
- 至少一次投递:投出去的任务在成功跑完前记 pending_delivery,异常回滚 restore。

线程只在 main 显式 `start()` 时启动,import 本模块零副作用 —— 冒烟测试可以只
调用纯函数,不起线程、不碰模型。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

DURABLE_PATH = Path.cwd() / ".scheduled_tasks.json"


@dataclass
class CronJob:
    """一条定时任务的账本。pending_delivery 是「投了但还没确认跑完」的标记。"""

    id: str
    cron: str
    prompt: str
    recurring: bool
    durable: bool
    pending_delivery: bool = False
    last_fired: str | None = None


# ---- cron 字段匹配与校验(纯函数,无副作用)----


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        return value % int(field[2:]) == 0
    if "," in field:
        return any(_cron_field_matches(part.strip(), value) for part in field.split(","))
    if "-" in field:
        start, end = field.split("-", 1)
        return int(start) <= value <= int(end)
    return value == int(field)


def cron_matches(cron_expr: str, moment: datetime) -> bool:
    """一个时刻是否命中 cron。day 与 weekday 的 `*` 二义性照 Vixie cron 惯例:
    两者都 `*` → 天天;只有一个是 `*` → 另一者独占;都不 `*` → 或。"""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, day, month, weekday = fields
    cron_weekday = (moment.weekday() + 1) % 7  # cron 的周日=0,Python 的周一=0
    if not (
        _cron_field_matches(minute, moment.minute)
        and _cron_field_matches(hour, moment.hour)
        and _cron_field_matches(month, moment.month)
    ):
        return False
    day_matches = _cron_field_matches(day, moment.day)
    weekday_matches = _cron_field_matches(weekday, cron_weekday)
    if day == "*" and weekday == "*":
        return True
    if day == "*":
        return weekday_matches
    if weekday == "*":
        return day_matches
    return day_matches or weekday_matches


def _validate_cron_field(field: str, minimum: int, maximum: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) <= 0:
            return f"Invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            error = _validate_cron_field(part.strip(), minimum, maximum)
            if error:
                return error
        return None
    if "-" in field:
        start, end = field.split("-", 1)
        if not start.isdigit() or not end.isdigit():
            return f"Invalid range: {field}"
        if int(start) > int(end):
            return f"Range start is greater than end: {field}"
        if int(start) < minimum or int(end) > maximum:
            return f"Range {field} is outside [{minimum}-{maximum}]"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    value = int(field)
    if value < minimum or value > maximum:
        return f"Value {value} is outside [{minimum}-{maximum}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    rules = [
        ("minute", 0, 59),
        ("hour", 0, 23),
        ("day-of-month", 1, 31),
        ("month", 1, 12),
        ("day-of-week", 0, 6),
    ]
    for field, (name, minimum, maximum) in zip(fields, rules):
        error = _validate_cron_field(field, minimum, maximum)
        if error:
            return f"{name}: {error}"
    return None


class CronScheduler:
    """定时任务注册表 + 调度/投递线程。进程内单例 CRON。"""

    POLL_SECONDS = 1.0     # 调度线程轮询间隔
    SCAN_SECONDS = 0.2     # 队列处理线程抢锁间隔

    def __init__(self, durable_path: Path | None = None) -> None:
        self.durable_path = durable_path or DURABLE_PATH
        self.scheduled_jobs: dict[str, CronJob] = {}
        self.cron_queue: list[CronJob] = []
        self.cron_lock = threading.RLock()
        self.agent_lock: threading.Lock | None = None
        self.turn_runner = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._start_lock = threading.Lock()

    # ---- 持久化 ----

    def _save_durable(self) -> None:
        payload = [asdict(job) for job in self.scheduled_jobs.values() if job.durable]
        temporary = self.durable_path.with_name(
            f"{self.durable_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(temporary, self.durable_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_durable(self) -> None:
        if not self.durable_path.exists():
            return
        try:
            payload = json.loads(self.durable_path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("expected a JSON list")
        except (OSError, json.JSONDecodeError, ValueError) as error:
            print(f"  [cron] could not load {self.durable_path.name}: {error}")
            return
        loaded = 0
        for item in payload:
            try:
                job = CronJob(**item)
                if validate_cron(job.cron):
                    raise ValueError(validate_cron(job.cron))
                if not job.id.startswith("cron_"):
                    raise ValueError("invalid job ID")
                if not job.prompt.strip():
                    raise ValueError("prompt cannot be empty")
            except (TypeError, ValueError) as error:
                print(f"  [cron] skipped invalid saved job: {error}")
                continue
            self.scheduled_jobs[job.id] = job
            if job.pending_delivery:
                self.cron_queue.append(job)
            loaded += 1
        if loaded:
            print(f"  [cron] loaded {loaded} durable job(s)")

    # ---- 纯操作(加锁,不进线程)----

    def _new_id(self) -> str:
        for _ in range(100):
            job_id = f"cron_{secrets.token_hex(4)}"
            if job_id not in self.scheduled_jobs:
                return job_id
        raise RuntimeError("could not allocate a cron job ID")

    def schedule(self, cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True):
        error = validate_cron(cron)
        if error:
            return error
        if not prompt.strip():
            return "Prompt cannot be empty"
        with self.cron_lock:
            job = CronJob(id=self._new_id(), cron=cron, prompt=prompt,
                          recurring=recurring, durable=durable)
            self.scheduled_jobs[job.id] = job
            try:
                if durable:
                    self._save_durable()
            except Exception:
                self.scheduled_jobs.pop(job.id, None)
                raise
        print(f"  [cron] scheduled {job.id}: {cron} -> {prompt[:60]}")
        return job

    def cancel(self, job_id: str) -> str:
        with self.cron_lock:
            job = self.scheduled_jobs.get(job_id)
            if job is None:
                return f"Job {job_id} not found"
            previous_queue = list(self.cron_queue)
            self.scheduled_jobs.pop(job_id)
            self.cron_queue[:] = [q for q in self.cron_queue if q.id != job_id]
            try:
                if job.durable:
                    self._save_durable()
            except Exception:
                self.scheduled_jobs[job_id] = job
                self.cron_queue[:] = previous_queue
                raise
        print(f"  [cron] cancelled {job_id}")
        return f"Cancelled {job_id}"

    def _enqueue_due(self, job: CronJob, minute_marker: str | None) -> None:
        old_pending, old_last = job.pending_delivery, job.last_fired
        job.pending_delivery = True
        if minute_marker is not None:
            job.last_fired = minute_marker
        try:
            if job.durable:
                self._save_durable()
        except Exception:
            job.pending_delivery, job.last_fired = old_pending, old_last
            raise
        self.cron_queue.append(job)

    def poll_due_jobs(self, moment: datetime) -> None:
        minute_marker = moment.strftime("%Y-%m-%d %H:%M")
        with self.cron_lock:
            for job in list(self.scheduled_jobs.values()):
                try:
                    if job.pending_delivery or job.last_fired == minute_marker:
                        continue
                    if cron_matches(job.cron, moment):
                        self._enqueue_due(job, minute_marker)
                        print(f"  [cron] due {job.id}: {job.prompt[:60]}")
                except Exception as error:
                    print(f"  [cron] could not enqueue {job.id}: {error}")

    def consume_cron_queue(self) -> list[CronJob]:
        with self.cron_lock:
            jobs = list(self.cron_queue)
            self.cron_queue.clear()
        return jobs

    def acknowledge_cron_jobs(self, jobs: list[CronJob]) -> None:
        """一个 scheduled turn 成功跑完:recurring 重置 pending,one-shot 删除。"""
        changed: list[tuple[CronJob, bool]] = []
        removed: list[CronJob] = []
        with self.cron_lock:
            for delivered in jobs:
                current = self.scheduled_jobs.get(delivered.id)
                if current is None:
                    continue
                changed.append((current, current.pending_delivery))
                if current.recurring:
                    current.pending_delivery = False
                else:
                    removed.append(current)
                    self.scheduled_jobs.pop(current.id)
            try:
                if any(job.durable for job, _ in changed):
                    self._save_durable()
            except Exception:
                for job in removed:
                    self.scheduled_jobs[job.id] = job
                for job, pending in changed:
                    job.pending_delivery = pending
                raise

    def restore_cron_jobs(self, jobs: list[CronJob]) -> None:
        """scheduled turn 跑崩了:把没跑成的任务放回队列。"""
        with self.cron_lock:
            queued = {job.id for job in self.cron_queue}
            for delivered in jobs:
                current = self.scheduled_jobs.get(delivered.id)
                if current is None:
                    continue
                current.pending_delivery = True
                if current.id not in queued:
                    self.cron_queue.append(current)
                    queued.add(current.id)

    def has_cron_queue(self) -> bool:
        with self.cron_lock:
            return bool(self.cron_queue)

    def list_jobs(self) -> list[CronJob]:
        with self.cron_lock:
            return list(self.scheduled_jobs.values())

    # ---- 线程(s15 main 显式启动)----

    def start(self, agent_lock: threading.Lock, turn_runner) -> None:
        """开两个 daemon 线程。turn_runner 是 main 注入的「把到点 prompt 跑进主循环」
        回调(已在 agent_lock 内被调用)。"""
        with self._start_lock:
            if self._started:
                return
            self._load_durable()
            self.agent_lock = agent_lock
            self.turn_runner = turn_runner
            self._stop.clear()
            self._threads = [
                threading.Thread(target=self._scheduler_loop, name="cron-scheduler", daemon=True),
                threading.Thread(target=self._queue_processor_loop, name="cron-queue-processor", daemon=True),
            ]
            for thread in self._threads:
                thread.start()
            self._started = True

    def stop(self) -> None:
        with self._start_lock:
            if not self._started:
                return
            self._stop.set()
            for thread in self._threads:
                thread.join(timeout=1)
            self._threads.clear()
            self._started = False

    def _scheduler_loop(self) -> None:
        while not self._stop.wait(self.POLL_SECONDS):
            self.poll_due_jobs(datetime.now())

    def _queue_processor_loop(self) -> None:
        while not self._stop.wait(self.SCAN_SECONDS):
            if not self.has_cron_queue():
                continue
            lock = self.agent_lock
            if lock is None or not lock.acquire(blocking=False):
                continue
            try:
                if self.has_cron_queue() and self.turn_runner is not None:
                    self.turn_runner()
            finally:
                lock.release()


CRON = CronScheduler()