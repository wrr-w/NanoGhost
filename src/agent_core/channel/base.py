# -*- coding: utf-8 -*-
"""Channel —— 通道适配器统一接口（P0）。

把「一种收发实现」（feishu / email / event / ...）抽象成一个 Channel：
  · send / reply / update —— 出站
  · capabilities          —— 能干什么（text / markdown / card / mention / image / ...）
  · start                 —— 入站（可选；长连接类通道才需要）

与既有 ChannelIO 的关系（不是替代）：
  ChannelIO 是「I/O 原语」（send_text / reply / send_images / reaction …），偏实现细节；
  Channel   是「对外统一的通道身份 + 能力」，用于注册表 / 端点目录。
  Channel 可以包一个 ChannelIO（见 feishu/channel.py）。

设计约束：本模块只依赖标准库，不 import 具体渠道，避免循环依赖。
"""

from __future__ import annotations

from abc import ABC
from typing import Any, Awaitable, Callable, Optional, Set


class Channel(ABC):
    """通道适配器基类。子类须给 name，并实现 send / reply。"""

    #: 通道类型名（命名空间前缀），如 "feishu"
    name: str = "unknown"

    # ── 出站 ──────────────────────────────────────────────

    def send(self, target: str, text: str) -> bool:
        """把纯文本发到某 target（渠道内地址，如飞书 chat_id）。"""
        raise NotImplementedError

    def reply(self, message_id: str, text: str) -> bool:
        """回复某条消息。"""
        raise NotImplementedError

    def update(self, message_id: str, text: str) -> bool:
        """原地更新（卡片 PATCH）。默认不支持。"""
        return False

    # ── 能力 ──────────────────────────────────────────────

    def capabilities(self) -> Set[str]:
        """能力集合。常见值：text / markdown / card / mention / image / reaction / update / thread"""
        return {"text"}

    # ── 入站（可选） ──────────────────────────────────────

    async def start(self, on_inbound: Optional[Callable[[Any], Awaitable[None]]] = None) -> None:
        """启动入站监听。默认无操作（由外部把消息交给通道）。"""
        return None

    async def stop(self) -> None:
        return None


__all__ = ["Channel"]
