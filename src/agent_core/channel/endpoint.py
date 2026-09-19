# -*- coding: utf-8 -*-
"""Endpoint —— 端点（P0）。

端点 = 一个可寻址的收发对象（人 / 群 / 通知渠道 / agent / 事件源 / 任务）。
地址 = "channel:target"（channel 是命名空间前缀，不是层级）。

例：
    feishu:oc_xxx      一个群（群里的人共享这个会话端点）
    feishu:ou_xxx      一个人
    event:src1         一个事件源
    agent:sess-X       一个 agent 会话
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Set


def make_addr(channel: str, target: str) -> str:
    """拼地址：('feishu', 'oc_x') -> 'feishu:oc_x'。"""
    return f"{channel}:{target}" if channel else str(target)


def parse_addr(addr: str):
    """拆地址：'feishu:oc_x' -> ('feishu', 'oc_x')；无冒号返回 ('', addr)。"""
    if not addr:
        return "", ""
    if ":" in addr:
        ch, _, tgt = addr.partition(":")
        return ch, tgt
    return "", addr


@dataclass
class Endpoint:
    """一个可寻址端点。"""

    addr: str
    channel: str = ""
    kind: str = "unknown"          # user | group | service | agent | event | task
    status: str = "online"         # online | offline | stale
    capabilities: Set[str] = field(default_factory=set)
    meta: Dict[str, Any] = field(default_factory=dict)
    first_seen: float = 0.0
    last_seen: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "addr": self.addr,
            "channel": self.channel,
            "kind": self.kind,
            "status": self.status,
            "capabilities": sorted(self.capabilities),
            "meta": dict(self.meta),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Endpoint {self.addr} kind={self.kind} status={self.status}>"


__all__ = ["Endpoint", "make_addr", "parse_addr"]
