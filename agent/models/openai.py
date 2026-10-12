from __future__ import annotations

import json

from agent.config import Config, ConfigError
from agent.models.base import Model

# OpenAI finish_reason → 通用语 stop_reason。
# 未知值原样透传:loop 的"异常终态"分支会兜住并安全收尾。
_FINISH_TO_STOP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}


class OpenAIModel(Model):
    """OpenAI 兼容方言(chat completions)。

    GPT / DeepSeek / Qwen / Ollama 同一个入口:换 base_url + MODEL_ID 即切换。
    通用语是 Anthropic 方言,所以这里进出各一道翻译:
        进:_messages_to_openai / _tools_to_openai
        出:_response_to_unified(finish_reason/tool_calls/usage 翻回统一契约)

    翻译层全是模块级纯函数:不碰 SDK、不打网络,冒烟测试直接喂假数据。
    SDK 只在 __init__ 里延迟导入——没装 openai 包的环境连本模块都能 import,
    只有真的构造 OpenAIModel 才会要这个包。
    """

    def __init__(self, config: Config) -> None:
        try:
            import openai  # 延迟导入:选 anthropic 的环境不需要装 openai
        except ImportError as exc:
            raise ConfigError(
                "MODEL_PROVIDER=openai requires the openai package "
                "(pip install openai)."
            ) from exc
        self._openai = openai
        self._client = openai.OpenAI(
            api_key=config.api_key,
            base_url=config.base_url or None,  # 空 → None → SDK 默认官方端点
        )
        self._model = config.model

    def complete(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        max_tokens: int = 8000,
    ) -> dict:
        request: dict = {
            "model": self._model,
            "messages": _messages_to_openai(messages, system),
        }
        if tools:
            # 空工具表不带走这个键:部分兼容网关收到 tools=[] 会报错
            request["tools"] = _tools_to_openai(tools)

        try:
            response = self._client.chat.completions.create(
                max_completion_tokens=max_tokens, **request
            )
        except self._openai.BadRequestError as exc:
            # 代差兜底:新参数 max_completion_tokens,老端点/部分兼容网关不认。
            # 错误文本点名了这个参数才降级重发(老参数 max_tokens),
            # 别的 400 一律原样上抛——不猜。
            if "max_completion_tokens" not in str(exc):
                raise
            response = self._client.chat.completions.create(
                max_tokens=max_tokens, **request
            )

        return _response_to_unified(response)


def _tools_to_openai(tools: list[dict]) -> list[dict]:
    """Anthropic 工具定义 → OpenAI function 定义。

    两边都是 JSON Schema,只是壳不同:input_schema 挪个位置叫 parameters。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def _messages_to_openai(messages: list[dict], system: str) -> list[dict]:
    """通用语消息 → OpenAI 消息。

    两条铁律:
    1) assistant 的每个 tool_use 必须翻成 tool_calls,且下一轮必须由
       role=tool 消息逐 id 回话——漏一条,下一次请求就 400。
       (loop 保证每个 tool_use 都有 tool_result,这里照翻即可。)
    2) thinking 块丢弃:OpenAI chat completions 没有这个概念,也不接受回传。
    """
    out: list[dict] = [{"role": "system", "content": system}]
    for msg in messages:
        role, content = msg["role"], msg["content"]
        if isinstance(content, str):  # 用户原话:两边同构,直通
            out.append({"role": role, "content": content})
            continue

        if role == "assistant":
            text = "".join(b["text"] for b in content if b["type"] == "text")
            m: dict = {"role": "assistant"}
            if text:
                m["content"] = text
            tool_calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {
                        "name": b["name"],
                        "arguments": json.dumps(b["input"]),
                    },
                }
                for b in content
                if b["type"] == "tool_use"
            ]
            if tool_calls:
                m["tool_calls"] = tool_calls
            if "content" not in m and "tool_calls" not in m:
                m["content"] = ""  # 双缺兜底:OpenAI 不收既无文本又无调用的消息
            out.append(m)
        else:  # user 的块列表 = 工具结果回传
            texts: list[str] = []
            for b in content:
                if b["type"] == "tool_result":
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": b["tool_use_id"],
                            "content": b["content"],
                        }
                    )
                elif b["type"] == "text":
                    texts.append(b["text"])
            if texts:
                out.append({"role": "user", "content": "\n".join(texts)})
    return out


def _response_to_unified(response) -> dict:
    """OpenAI 响应对象 → 统一响应契约(base.Model 的返回 shape)。

    只做属性访问,不 import SDK:测试用 SimpleNamespace 造假就能跑。
    """
    choice = response.choices[0]
    content: list[dict] = []
    if choice.message.content:
        content.append({"type": "text", "text": choice.message.content})
    for tc in choice.message.tool_calls or []:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            # 参数是 JSON 字符串,被 max_completion_tokens 截断时就是坏的。
            # 仍产出 tool_use,input 回填 {}:每个 tool_call 必须有回话,
            # 漏了协议脱节。工具拿到空参数会自己报错,模型看见错误结果
            # 会重新发起调用——对话自愈,协议不断。
            args = {}
        content.append(
            {"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": args}
        )
    return {
        "stop_reason": _FINISH_TO_STOP.get(choice.finish_reason, choice.finish_reason),
        "content": content,
        "usage": {
            "input": response.usage.prompt_tokens,
            "output": response.usage.completion_tokens,
        },
    }
