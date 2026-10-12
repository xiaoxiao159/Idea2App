from __future__ import annotations

from anthropic import Anthropic

from agent.config import Config
from agent.models.base import Model

DROP_THINKING = True


class AnthropicModel(Model):
    """Anthropic Messages 方言。

    通用语就是照着它定义的,所以这个 adapter 零翻译:进出的格式
    = 契约本身。归一化循环只是把 SDK 的 pydantic 对象转成普通 dict,
    让 loop 和测试从此不认识任何 SDK 类型。
    """

    def __init__(self, config: Config) -> None:
        self._client = Anthropic(api_key=config.api_key, base_url=config.base_url)
        self._model = config.model

    def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        max_tokens: int = 8000,
    ) -> dict:
        response = self._client.messages.create(
            model=self._model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
        )

        content: list[dict] = []
        for block in response.content:
            if block.type == "text":
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
            elif block.type == "thinking":
                if not DROP_THINKING:
                    content.append(
                        {
                            "type": "thinking",
                            "thinking": block.thinking,
                            "signature": block.signature,
                        }
                    )
            else:
                # 未知块类型(如 redacted_thinking):原样保留。
                # SDK 的 block 是 pydantic 模型,model_dump() 能无损转 dict。
                content.append(block.model_dump())

        return {
            "stop_reason": response.stop_reason,
            "content": content,
            "usage": {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
        }
