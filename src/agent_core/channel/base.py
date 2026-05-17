"""ChannelPort 抽象基类。

Channel 是 Agent 与外部 IM/Web 的通信桥梁。
Agent 本身不关心消息来源,Channel 负责接收事件并调用 Agent。
"""

from abc import ABC, abstractmethod
from typing import Any


class ChannelPort(ABC):
    """通道端口。每个 Channel 负责一个消息来源。"""

    @abstractmethod
    async def start(self) -> None:
        """启动通道监听。"""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """停止通道监听。"""
        ...

    @abstractmethod
    async def run_forever(self) -> None:
        """持续运行通道。"""
        ...
