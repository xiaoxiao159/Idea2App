"""cron 工具:schedule_cron / list_crons / cancel_cron(schema + handler + display)。

薄壳:引擎与状态在 cron.py;这里只做 schema 翻译、把 CronScheduler 的返回捏成
(text, is_error)。schedule_cron 返回 CronJob 或错误串,CronJob 不是 dict,单独判断。
"""
from __future__ import annotations

from typing import Any

from agent import cron
from agent.tools.base import Tool


def _schedule(params: dict[str, Any]) -> tuple[str, bool]:
    expression = str(params.get("cron", "")).strip()
    prompt = str(params.get("prompt", "")).strip()
    recurring = bool(params.get("recurring", True))
    durable = bool(params.get("durable", True))
    result = cron.CRON.schedule(expression, prompt, recurring, durable)
    if isinstance(result, str):
        return (f"Error: {result}", True)
    return (f"Scheduled {result.id}: {expression} -> {prompt}", False)


def _list(params: dict[str, Any]) -> tuple[str, bool]:
    jobs = cron.CRON.list_jobs()
    if not jobs:
        return ("No cron jobs.", False)
    lines = [
        f"{job.id}: {job.cron} -> {job.prompt[:60]} "
        f"[{'recurring' if job.recurring else 'one-shot'}, "
        f"{'durable' if job.durable else 'session'}]"
        for job in jobs
    ]
    return ("\n".join(lines), False)


def _cancel(params: dict[str, Any]) -> tuple[str, bool]:
    text = cron.CRON.cancel(str(params.get("job_id", "")))
    is_error = text.startswith("Job ") and "not found" in text
    return (text, is_error)


def _disp_schedule(params: dict[str, Any]) -> str:
    return f"cron+ {params.get('cron', '?')} -> {params.get('prompt', '')}"


def _disp_list(params: dict[str, Any]) -> str:
    return "cron list"


def _disp_cancel(params: dict[str, Any]) -> str:
    return f"cron- {params.get('job_id', '?')}"


SCHEDULE_CRON_TOOL = Tool(
    schema={
        "name": "schedule_cron",
        "description": (
            "Schedule a prompt to run at a future local time using a 5-field cron "
            "expression (minute hour day-of-month month day-of-week). Use '*' for any, "
            "*/N for every N, N for exact, N-M for range, N,M for a set."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cron": {"type": "string", "description": "5-field cron expression"},
                "prompt": {"type": "string", "description": "the prompt to deliver when due"},
                "recurring": {"type": "boolean", "description": "true = repeat, false = one-shot (default true)"},
                "durable": {"type": "boolean", "description": "true = survive restart (default true)"},
            },
            "required": ["cron", "prompt"],
        },
    },
    handler=_schedule,
    display=_disp_schedule,
)

LIST_CRONS_TOOL = Tool(
    schema={
        "name": "list_crons",
        "description": "List all scheduled cron jobs.",
        "input_schema": {"type": "object", "properties": {}},
    },
    handler=_list,
    display=_disp_list,
)

CANCEL_CRON_TOOL = Tool(
    schema={
        "name": "cancel_cron",
        "description": "Cancel a scheduled cron job by its id.",
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    handler=_cancel,
    display=_disp_cancel,
)