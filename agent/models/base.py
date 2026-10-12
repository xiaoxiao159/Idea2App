from __future__ import annotations

from abc import ABC, abstractmethod


class Model(ABC):
    """所有模型 adapter 的契约:全项目对"一次补全"的唯一认知。

    输入(通用语 = Anthropic 方言,loop 和 tools 已经在说的语言):
        messages — [{"role": "user"|"assistant", "content": str | list[块]}]
                   块:{"type": "text", "text"} |
                       {"type": "tool_use", "id", "name", "input"} |
                       {"type": "tool_result", "tool_use_id", "content", ["is_error"]}
        system   — 系统提示词,顶层参数,不塞进 messages
        tools    — Anthropic 格式工具定义 [{"name", "description", "input_schema"}]

    输出(统一响应,smoke 测试的 fake 复刻的就是这个 shape):
        {
          "stop_reason": "end_turn" | "tool_use" | "max_tokens" | <未知透传>,
          "content": [ {"type": "text", ...} | {"type": "tool_use", ...} ],
          "usage": {"input": int, "output": int},
        }

    为什么是 ABC 而不是只写文档:子类漏实现 complete,在实例化那一刻
    就炸(TypeError),而不是等到第一次调用;契约同时是运行时检查。
    """

    @abstractmethod
    def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        max_tokens: int = 8000,
    ) -> dict:
        """跑一次补全,返回统一响应 dict。网络之外不许有副作用。"""
