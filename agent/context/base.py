"""context 地基的数据形状:窗口条目 / 档案块 / 预算。

三个 dataclass 全是纯数据,不干活、不持状态——跟 config._ProviderSpec 一个德行:
形状住这里,行为在 meter.py(纯函数)和 __init__.py(状态与装配)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Entry:
    """窗口里的一条:内容(API 原样)+ 账(给计量与将来的动作记账)。

    pin:True = 压不得也删不得(用户原话、护栏注入);False = 可被压缩/淘汰。
    epoch:属于第几压缩轮。现在恒 0,压缩节才 +1 —— 先留字段,不留行为。
    """

    msg: dict[str, Any]   # {"role", "content"};content 是 str 或块列表
    tokens: int
    pin: bool
    epoch: int


@dataclass(frozen=True)
class Block:
    """被移出窗口的原件索引。内容躺在磁盘(archive/),这里只记地址与账。

    path:cwd 下的相对路径;读 archive.read(block) 才取回内容——不常驻内存。
    """

    kind: str      # "raw_turn" | "summary" | "tool_result"
    tokens: int    # 原件估算 token
    path: str      # 磁盘位置;内容靠 read 取回


@dataclass(frozen=True)
class Budget:
    """窗口预算。window/max_tokens/reserve_ratio 是输入;input_budget 是算出来的。"""

    window: int
    max_tokens: int
    reserve_ratio: float

    @property
    def input_budget(self) -> int:
        """真正能塞输入的额度。输出和输入抢同一个窗口,必须先扣掉输出预留。"""
        return int(self.window * (1 - self.reserve_ratio)) - self.max_tokens