# -*- coding: utf-8 -*-
"""ChannelRegistry —— 通道适配器注册表（P0）。

管理「通道类型 / adapter」：name -> Channel 实例。
  register(channel)  ·  get(name)  ·  names()  ·  all()

模块级提供一个默认单例（REGISTRY），并导出便捷函数，
方便运行时在启动处注册、在别处取用。
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

from .base import Channel

logger = logging.getLogger("agent_core")


class ChannelRegistry:
    """通道适配器注册表（线程安全）。"""

    def __init__(self) -> None:
        self._channels: Dict[str, Channel] = {}
        self._lock = threading.Lock()

    def register(self, channel: Channel) -> Channel:
        name = getattr(channel, "name", "") or "unknown"
        with self._lock:
            self._channels[name] = channel
        logger.info("[ChannelRegistry] registered channel=%s", name)
        return channel

    def get(self, name: str) -> Optional[Channel]:
        with self._lock:
            return self._channels.get(name)

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._channels.keys())

    def all(self) -> List[Channel]:
        with self._lock:
            return list(self._channels.values())

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None


# ── 模块级默认单例 ───────────────────────────────────────
REGISTRY = ChannelRegistry()


def get_registry() -> ChannelRegistry:
    return REGISTRY


def register_channel(channel: Channel) -> Channel:
    return REGISTRY.register(channel)


def get_channel(name: str) -> Optional[Channel]:
    return REGISTRY.get(name)


__all__ = [
    "ChannelRegistry",
    "REGISTRY",
    "get_registry",
    "register_channel",
    "get_channel",
]
