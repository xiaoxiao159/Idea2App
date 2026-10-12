"""cron (s12) 冒烟:字段匹配/校验/持久化/到期入队/acknowledge/restore。纯函数+账本,不起线程。"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(tempfile.mkdtemp())  # 先换目录再 import,DURABLE_PATH 才落在临时目录

from agent.cron import CronScheduler, cron_matches, validate_cron  # noqa: E402

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok - {name}")


# ---- 1. validate_cron:合法放行 / 五类非法 ----
assert validate_cron("0 9 * * *") is None
assert validate_cron("*/15 9 * * *") is None
assert validate_cron("0 9 1-15 * *") is None
assert validate_cron("0 9 1,15 * *") is None
assert validate_cron("5 9 * * 0") is None
for bad, needle in (
    ("", "5 fields"),
    ("0 9 *", "5 fields"),
    ("60 9 * * *", "outside"),
    ("0 24 * * *", "outside"),
    ("0 9 * 13 *", "outside"),
    ("0 9 * * 7", "outside"),
    ("*/0 9 * * *", "Invalid step"),
    ("0 9 15-1 * *", "greater than end"),
):
    err = validate_cron(bad)
    assert err is not None and needle in err, (bad, err)
ok("validate_cron: 合法放行,字段数/越界/步长/反向区间全拦")

# ---- 2. cron_matches:minute/hour/day/weekday 四轴 + 非法表达式 ----
MON = datetime(2026, 1, 5, 9, 0)   # 周一
SUN = datetime(2026, 1, 4, 9, 0)   # 周日
assert cron_matches("0 9 * * *", MON) is True
assert cron_matches("0 9 * * *", datetime(2026, 1, 5, 10, 0)) is False
assert cron_matches("*/15 9 * * *", datetime(2026, 1, 5, 9, 30)) is True
assert cron_matches("*/15 9 * * *", datetime(2026, 1, 5, 9, 31)) is False
assert cron_matches("0 9 * * 0", SUN) is True
assert cron_matches("0 9 * * 0", MON) is False
assert cron_matches("0 9 15 * *", datetime(2026, 1, 15, 9, 0)) is True
assert cron_matches("0 9 15 * *", datetime(2026, 1, 16, 9, 0)) is False
assert cron_matches("0 9 *", MON) is False  # 字段数不对 → 不匹配
ok("cron_matches: 小时/分钟/步长/星期/日期 + 坏表达式")

# ---- 3. schedule / 持久化 / 重载 / cancel ----
sched = CronScheduler()
job = sched.schedule("0 9 * * *", "morning", recurring=True, durable=True)
assert job.id.startswith("cron_")
assert isinstance(sched.schedule("bad", "x"), str)
assert sched.schedule("0 9 * * *", "   ") == "Prompt cannot be empty"
assert Path(".scheduled_tasks.json").is_file()
ok("schedule: 合法返回 CronJob,非法/空 prompt 返回错误串,durable 落盘")

fresh = CronScheduler()
fresh._load_durable()
assert job.id in fresh.scheduled_jobs
ok("reload: durable 任务跨实例找回")

assert any(j.id == job.id for j in sched.list_jobs())
assert sched.cancel(job.id).startswith("Cancelled")
assert sched.cancel("cron_deadbeef") == "Job cron_deadbeef not found"
assert job.id not in [j.id for j in sched.list_jobs()]
ok("cancel / list")

# ---- 4. 到期入队 + acknowledge(recurring)/restore ----
q = CronScheduler()
one = q.schedule("*/1 * * * *", "ping", recurring=False, durable=False)
q.poll_due_jobs(datetime(2026, 1, 5, 9, 7))
jobs = q.consume_cron_queue()
assert [j.id for j in jobs] == [one.id] and one.pending_delivery is True
ok("poll: 到期入队 + 标 pending_delivery")

rec = q.schedule("*/1 * * * *", "tick", recurring=True, durable=False)
q.poll_due_jobs(datetime(2026, 1, 5, 9, 8))
rj = q.consume_cron_queue()
assert [j.id for j in rj] == [rec.id]
q.acknowledge_cron_jobs(rj)
assert rec.pending_delivery is False and rec.id in q.scheduled_jobs
ok("acknowledge: recurring 重置 pending 且不删")

one2 = q.schedule("*/1 * * * *", "ping2", recurring=False, durable=False)
q.poll_due_jobs(datetime(2026, 1, 5, 9, 9))
o2 = q.consume_cron_queue()
q.restore_cron_jobs(o2)
assert one2.pending_delivery is True and q.has_cron_queue() is True
ok("restore: 崩掉的投递回队列(至少一次)")

print(f"\nALL {PASS} assertions passed")