# -*- coding: utf-8 -*-
"""收件箱（每端点队列）—— P1。

设计要点（见 docs/channels_and_router.md §6）：
  · 每个端点（target）一个【入站队列】：FIFO、不可丢（≠ 缓存）
  · 所有入口（通道消息 / 定时 / 事件）先 submit 到这里，再由常驻消费者取走

事件 = 统一信封 + 任意 payload：
    InboxEvent { id, target, kind, source, summary, ts, payload }

线程安全：生产者可能在任意线程（SDK 回调线程 / 定时线程 / 其它），
消费者在自己的线程里消费。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agent_core")


@dataclass
class InboxEvent:
    """统一信封 + 任意内容。

    target  : 端点地址（如 "feishu:oc_xxx" / "sched:daily"）
    kind    : 事件类别（channel_message / timer / event / ...），消费者据此分派
    payload : 任意内容（agent / handler 自己解释）
    """

    target: str
    kind: str = "event"
    payload: Any = None
    source: str = ""
    summary: str = ""
    id: str = ""
    ts: float = 0.0

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.ts:
            self.ts = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "kind": self.kind,
            "source": self.source,
            "summary": self.summary,
            "ts": self.ts,
        }


class Inbox:
    """单端点 FIFO 队列（线程安全）。"""

    def __init__(self) -> None:
        self._q: deque = deque()
        self._lock = threading.Lock()

    def put(self, event: InboxEvent) -> None:
        with self._lock:
            self._q.append(event)

    def drain(self, kinds: Optional[set] = None) -> List[InboxEvent]:
        """取走队列（FIFO）。给定 kinds 则只取这些类别，其余留下（保序）。"""
        with self._lock:
            if kinds is None:
                items = list(self._q)
                self._q.clear()
                return items
            keep: deque = deque()
            take: List[InboxEvent] = []
            while self._q:
                ev = self._q.popleft()
                (take if ev.kind in kinds else keep).append(ev)
            self._q = keep
            return take

    def size(self) -> int:
        with self._lock:
            return len(self._q)


class InboxHub:
    """端点 → 收件箱 的集合 + 全局通知（线程安全）。"""

    def __init__(self) -> None:
        self._inboxes: Dict[str, Inbox] = {}
        self._lock = threading.Lock()
        self._notify = threading.Event()

    # ── 内部 ──
    def _box(self, target: str) -> Inbox:
        with self._lock:
            box = self._inboxes.get(target)
            if box is None:
                box = Inbox()
                self._inboxes[target] = box
            return box

    # ── 生产 ──
    def submit(self, event: InboxEvent) -> InboxEvent:
        if not event.target:
            raise ValueError("event.target is required")
        self._box(event.target).put(event)
        self._notify.set()
        logger.info(
            "[Inbox] +event target=%s kind=%s id=%s summary=%s",
            event.target, event.kind, event.id, event.summary or "-",
        )
        return event

    def wake(self) -> None:
        """唤醒消费者（例如 worker 刚清空 busy 时补一次）。"""
        self._notify.set()

    def wait(self, timeout: float = 1.0) -> bool:
        fired = self._notify.wait(timeout)
        self._notify.clear()
        return fired

    # ── 消费 / 观测 ──
    def drain(self, target: str, kinds: Optional[set] = None) -> List[InboxEvent]:
        with self._lock:
            box = self._inboxes.get(target)
        return box.drain(kinds) if box else []

    def size(self, target: str) -> int:
        with self._lock:
            box = self._inboxes.get(target)
        return box.size() if box else 0

    def targets(self) -> List[str]:
        with self._lock:
            return list(self._inboxes.keys())

    def total(self) -> int:
        with self._lock:
            boxes = list(self._inboxes.values())
        return sum(b.size() for b in boxes)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {t: b.size() for t, b in self._inboxes.items()}


# ── 模块级默认单例 ───────────────────────────────────────
HUB = InboxHub()


def get_hub() -> InboxHub:
    return HUB


def submit_event(
    target: str,
    kind: str = "event",
    payload: Any = None,
    source: str = "",
    summary: str = "",
) -> InboxEvent:
    """便捷：造一条事件并投进收件箱。"""
    return HUB.submit(
        InboxEvent(target=target, kind=kind, payload=payload, source=source, summary=summary)
    )


__all__ = [
    "InboxEvent",
    "Inbox",
    "InboxHub",
    "HUB",
    "get_hub",
    "submit_event",
]
