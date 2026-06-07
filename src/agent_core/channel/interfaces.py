# -*- coding: utf-8 -*-
"""渠道接口定义。

ChannelPort — 通道生命周期（编排器实现）
ChannelIO  — 渠道 I/O 操作（IO 层实现）

agent 循环只依赖 ChannelIO，不依赖具体平台。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple


class ChannelPort(ABC):
    """通道端口。每个 Channel 负责一个消息来源的启停。"""

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...

    @abstractmethod
    async def run_forever(self) -> None:
        ...


class ChannelIO(ABC):
    """渠道 I/O 接口。

    agent 循环只依赖此接口，不依赖具体平台。
    """

    @abstractmethod
    def send_text(self, chat_id: str, text: str) -> bool:
        ...

    @abstractmethod
    def reply(self, message_id: str, text: str) -> bool:
        ...

    @abstractmethod
    def send_images(self, chat_id: str, b64_list: List[str]) -> Dict[str, Any]:
        ...

    @abstractmethod
    def add_reaction(self, message_id: str) -> str:
        """返回 reaction_id 供后续删除。"""
        ...

    @abstractmethod
    def delete_reaction(self, message_id: str, reaction_id: str) -> bool:
        ...

    @abstractmethod
    def download_image(self, message_id: str, file_key: str) -> Optional[Tuple[bytes, str]]:
        """下载图片，返回 (bytes, content_type) 或 None。"""
        ...
