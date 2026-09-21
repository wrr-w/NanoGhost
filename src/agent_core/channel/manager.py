# -*- coding: utf-8 -*-
"""ChannelManager —— 通道与端点的统一控制面。

在 `ChannelRegistry` 与 `EndpointDirectory` 之上补一层轻量治理：
  · register_channel / register_endpoint
  · enable/disable channel / endpoint
  · can_send_text()：给 Router 的统一发送守卫
  · health()：为后续健康检查与观测预留
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .directory import EndpointDirectory, get_directory
from .endpoint import Endpoint
from .route import resolve_reply_policy
from .registry import ChannelRegistry, get_registry


class ChannelManager:
    def __init__(
        self,
        registry: Optional[ChannelRegistry] = None,
        directory: Optional[EndpointDirectory] = None,
    ) -> None:
        self.registry = registry if registry is not None else get_registry()
        self.directory = directory if directory is not None else get_directory()
        self._channel_enabled: Dict[str, bool] = {}
        self._endpoint_enabled: Dict[str, bool] = {}
        self._health: Dict[str, Dict[str, Any]] = {}

    def register_channel(self, channel):
        return self.registry.register(channel)

    def register_endpoint(self, addr: str, **kw) -> Endpoint:
        return self.directory.register(addr, **kw)

    def get_endpoint(self, addr: str) -> Optional[Endpoint]:
        return self.directory.get(addr)

    def enable_channel(self, name: str, enabled: bool) -> None:
        self._channel_enabled[name] = bool(enabled)

    def enable_endpoint(self, addr: str, enabled: bool) -> None:
        self._endpoint_enabled[addr] = bool(enabled)

    def is_channel_enabled(self, name: str) -> bool:
        return self._channel_enabled.get(name, True)

    def is_endpoint_enabled(self, addr: str) -> bool:
        return self._endpoint_enabled.get(addr, True)

    def set_health(self, key: str, status: Dict[str, Any]) -> None:
        self._health[key] = dict(status)

    def health(self, key: str) -> Dict[str, Any]:
        return dict(self._health.get(key, {}))

    def can_send_text(self, addr: str) -> tuple[bool, str]:
        ep = self.get_endpoint(addr)
        if ep is None:
            return True, ""
        if not self.is_endpoint_enabled(addr):
            return False, "endpoint_disabled"
        if ep.channel and not self.is_channel_enabled(ep.channel):
            return False, "channel_disabled"
        if ep.status and ep.status not in ("online",):
            return False, f"endpoint_status:{ep.status}"
        if ep.capabilities and "text" not in ep.capabilities:
            return False, "missing_capability:text"
        return True, ""

    def resolve_delivery_policy(self, addr: str, channel) -> dict:
        ep = self.get_endpoint(addr)
        return resolve_reply_policy(
            endpoint=ep,
            channel_policy=(channel.default_delivery_policy() if channel else {}),
        )


MANAGER = ChannelManager()


def get_channel_manager() -> ChannelManager:
    return MANAGER


__all__ = ["ChannelManager", "MANAGER", "get_channel_manager"]
