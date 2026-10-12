"""Idea2App:在 agent 引擎(agent/)之上封装的两阶段流程,agent 一个字节不改。

阶段:IDEA → PRD(草稿→人审)→ SPEC(草稿→人审=冻结)→ EXECUTING(自驱)→ DONE。
本包只提供流程层(skill 安装、阶段机、文档审批门、宿主);引擎的能力
(skill / tool / hook / task / team / workflow / goal)全部复用 agent/。
"""
__version__ = "0.1.0"