# -*- coding: utf-8 -*-
"""EndpointDirectory —— 端点目录（P0）。

「动态注册」：任何东西都能登记成一个端点；登记后有地址、可枚举、可查状态。
目录 = 注册表的视图（= 模型自由路由的「菜单」来源）。

模块级提供一个默认单例（DIRECTORY），并导出便捷函数。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Set

from .endpoint import Endpoint, parse_addr

logger = logging.getLogger("agent_core")


class EndpointDirectory:
    """端点目录（线程安全）。注册幂等：重复注册只更新 last_seen / meta / status。"""

    def __init__(self) -> None:
        self._eps: Dict[str, Endpoint] = {}
        self._lock = threading.RLock()

    # ── 写 ──────────────────────────────────────────────

    def register(
        self,
        addr: str,
        channel: str = "",
        kind: str = "unknown",
        capabilities: Optional[Iterable[str]] = None,
        meta: Optional[Dict[str, Any]] = None,
        status: str = "online",
    ) -> Endpoint:
        """注册 / 更新一个端点。"""
        if not addr:
            raise ValueError("addr is required")
        if not channel:
            channel, _ = parse_addr(addr)
        now = time.time()
        with self._lock:
            ep = self._eps.get(addr)
            if ep is None:
                ep = Endpoint(addr=addr, channel=channel, kind=kind, status=status,
                              first_seen=now, last_seen=now)
                self._eps[addr] = ep
                logger.info("[Directory] +endpoint %s (kind=%s)", addr, kind)
            if capabilities:
                ep.capabilities = set(capabilities)
            if meta:
                ep.meta.update(meta)
            if kind and kind != "unknown":
                ep.kind = kind
            if status:
                ep.status = status
            ep.last_seen = now
            return ep

    def set_status(self, addr: str, status: str) -> bool:
        with self._lock:
            ep = self._eps.get(addr)
            if not ep:
                return False
            ep.status = status
            return True

    def unregister(self, addr: str) -> bool:
        with self._lock:
            return self._eps.pop(addr, None) is not None

    # ── 读 ──────────────────────────────────────────────

    def get(self, addr: str) -> Optional[Endpoint]:
        with self._lock:
            return self._eps.get(addr)

    def list(
        self,
        channel: Optional[str] = None,
        kind: Optional[str] = None,
        q: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Endpoint]:
        with self._lock:
            out = list(self._eps.values())
        if channel:
            out = [e for e in out if e.channel == channel]
        if kind:
            out = [e for e in out if e.kind == kind]
        if status:
            out = [e for e in out if e.status == status]
        if q:
            ql = q.lower()
            out = [
                e for e in out
                if ql in e.addr.lower()
                or ql in str(e.meta.get("name", "")).lower()
                or ql in str(e.meta.get("chat_name", "")).lower()
            ]
        return out

    def count(self) -> int:
        with self._lock:
            return len(self._eps)

    def __contains__(self, addr: str) -> bool:
        return self.get(addr) is not None

    def __len__(self) -> int:
        return self.count()


# ── 模块级默认单例 ───────────────────────────────────────
DIRECTORY = EndpointDirectory()


def get_directory() -> EndpointDirectory:
    return DIRECTORY


def register_endpoint(addr: str, **kw) -> Endpoint:
    return DIRECTORY.register(addr, **kw)


def list_endpoints(**kw) -> List[Endpoint]:
    return DIRECTORY.list(**kw)


__all__ = [
    "EndpointDirectory",
    "DIRECTORY",
    "get_directory",
    "register_endpoint",
    "list_endpoints",
]
