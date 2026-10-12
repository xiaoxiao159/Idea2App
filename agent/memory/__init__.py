"""memory 包:进程死亡后还能找回来的东西 —— 会话转录 + 跨会话记忆(约定/事实)。

跟 context/ 的分工:context 管"窗口里现在让模型看什么"(进程死即亡),
这里管"沉到磁盘、下一进程还能捡起来的"(活过进程)。

四件套各自的落点:
  约定文件  agentsmd —— 人写的 AGENTS.md,常驻 system prompt(程序+项目约定)
  会话转录  sessions —— 每条往返 append-only JSONL,默认续最近,--resume 挑旧的(情景)
  事实记忆  facts    —— profile/project/entities 的 markdown,启动注入、退出沉淀(语义)
  压缩摘要  不另设模块 —— context/compact 的梯子第 2 档负责,摘要进窗口、原文进档案
"""
from agent.memory import agentsmd, facts, sessions

__all__ = ["agentsmd", "facts", "sessions"]