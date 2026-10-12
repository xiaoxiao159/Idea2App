from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

ToolHandler = Callable[[dict[str, Any]], tuple[str, bool]]
ToolDisplay = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class Tool:
    """
    schema 给模型看,handler 给机器跑,display 给人看
    """

    schema: dict[str, Any]
    handler: ToolHandler
    display: ToolDisplay | None = None

    def shown(self, params: dict[str, Any]) -> str:
        """调用摘要。没配 display 的工具兜底:工具名 + 参数键名。"""
        if self.display is not None:
            return self.display(params)
        return f"{self.schema['name']}({', '.join(params)})"
