# -*- coding: utf-8 -*-
"""runtime —— 运行时的通用件（P1）。

  · inbox    : 每端点队列（收件箱）+ 事件信封
  · consumer : 常驻消费者（按端点忙闲派活）
"""

from .inbox import (
    Inbox,
    InboxEvent,
    InboxHub,
    HUB,
    get_hub,
    submit_event,
)
from .consumer import ResidentConsumer
from .subagent_pool import SubAgentPool, get_pool, reset_pool
from .watcher import Watch, Watcher, get_watcher, reset_watcher

__all__ = [
    "Inbox",
    "InboxEvent",
    "InboxHub",
    "HUB",
    "get_hub",
    "submit_event",
    "ResidentConsumer",
    "SubAgentPool",
    "get_pool",
    "reset_pool",
    "Watch",
    "Watcher",
    "get_watcher",
    "reset_watcher",
]
