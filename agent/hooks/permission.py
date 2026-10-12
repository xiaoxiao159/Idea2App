"""权限闸门(PreToolUse 消费者):三态策略 + REPL 提问 + 会话内授权。

优先级 deny > allow > ask——自由可以给,边界不能让:
- deny floor:灭家级命令子串匹配,不可谈判,a 过的授权也拦不住它
- allow:read 无条件放行;会话内 a 过的授权(_GRANTS,进程退出即清零)
- ask:REPL 提问 y/a/n;Enter / Ctrl+C / Ctrl+Z 一律当 n(安全侧:没说同意就是没同意)

ask 完全藏在闸门内部:循环只看得见放行(None)与否决(理由字符串)。
"""
from __future__ import annotations

from typing import Any

from agent import mcp
from agent import tools

# ---- 策略表(v1 写死:唯一用户是本人,配置文件等真实差异清单出现再说)----

# 无条件放行:纯读(read/skill)+ 纯内存写(todo_write)+ 内部腾地(compact)+
# 委派(task 本身不碰文件,子 agent 的动作另过闸门)+ 任务图(task_* 只写 .tasks/,
# 是 agent 自己的协调面)。逐个 claim/complete 都问人会把多步工作卡死,故全放行。
ALLOW_TOOLS = (
    "read", "todo_write", "skill", "compact", "task",
    "create_task", "update_task", "list_tasks", "get_task",
    "claim_task", "complete_task",
    # s12:定时任务只改调度器自己的协调面(.scheduled_tasks.json + 内存)
    "schedule_cron", "list_crons", "cancel_cron",
    # s14:连接进程内 mock server 本身无副作用;真正的 mcp__* 调用走 host policy
    "connect_mcp",
    # s13:团队协调(不碰文件、不 spawn 重活,只改队友线程状态;plan 闸门另在第一线)
    "list_teammates", "request_shutdown", "request_plan", "review_plan", "send_message",
)

# 灭家级底线,子串匹配。从 s01 的 _DANGEROUS 原样迁来:换住处(bash.py→这里)、换身份
# (黑名单挡一切 → 优先级最高、a 授权也绕不过的底线)。底线要小而硬,加戏=第二张打地鼠名单。
DENY_FLOOR = ("rm -rf /", "sudo ", "shutdown", "reboot", "mkfs", ":(){ :|:& };:")

# 会话内 a 授权,进程退出即清零。键:write / edit / "bash:程序名"(命令第一个词)。
# 粒度拍过板:程序名意味着 a 过 git 后 push --force 也免问——粗是故意的,真疼了再进化成参数级。
_GRANTS: set[str] = set()


def _ask(shown: str) -> str:
    """REPL 提问,返回 y/a/n。任何异常输入都落 n。"""
    print(f"  [permission] {shown}")
    try:
        answer = input("  allow? [y]once / [a]lways this session / [n]o (Enter=n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "n"
    if answer in ("y", "a"):
        return answer
    return "n"


def check(name: str, params: dict[str, Any]) -> str | None:
    """PreToolUse 消费者:默许 None / 否决理由 str。检查顺序就是优先级的代码化。"""
    if name not in tools.REGISTRY:
        return None  # 幻觉工具:不存在的能力无从授权,交给 s02 的 unknown tool 报错
    if name in ALLOW_TOOLS:
        return None

    # s14:mcp__* 工具走 host policy:allow 直放、deny 不可谈判、confirm 走会话内授权
    if name.startswith("mcp__"):
        policy = mcp.policy_for(name)
        if policy == "allow":
            return None
        if policy == "deny":
            return (
                f"deny: MCP tool '{name}' is disabled by host policy. "
                "This is not negotiable; find another way."
            )
        if name in _GRANTS:
            return None
        answer = _ask(tools.summary(name, params))
        if answer == "a":
            _GRANTS.add(name)
        if answer in ("y", "a"):
            return None
        return (
            f"user denied this {name} call. Do not retry the same call as-is; "
            "ask, adjust, or find another way."
        )

    if name == "bash":
        command = str(params.get("command", ""))
        for bad in DENY_FLOOR:
            if bad in command:
                return (
                    f"deny floor: command matched '{bad}'. "
                    "This is not negotiable; find another way."
                )
        words = command.split()
        if not words:
            return None  # 空命令:权限不是参数校验器,让 handler 的报错说话
        key = "bash:" + words[0]
    else:
        key = name  # write / edit 按工具名

    if key in _GRANTS:
        return None

    answer = _ask(tools.summary(name, params))
    if answer == "a":
        _GRANTS.add(key)
    if answer in ("y", "a"):
        return None
    # 措辞是给模型的指令,不是给人的:拒绝要明说,并封死"原样重试"这条路
    return (
        f"user denied this {name} call. Do not retry the same call as-is; "
        "ask, adjust, or find another way."
    )
