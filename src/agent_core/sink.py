# -*- coding: utf-8 -*-
"""Sink —— 结果 / 事件的出口（B：结果解耦）（P4）。

设计（见 docs/channels_and_router.md §8）：
    任务 / agent 的产出 = **结果 / 事件** → 落 sink（DB / 日志 / 总线）
    **渠道是订阅者** —— 任务自己不发消息。

Sink 是最小实现：
    · 环形缓冲（内存，recent/count）
    · 可选落盘（JSONL 追加，path 给定则写）
    · 订阅者（fn(record)）—— 渠道 / 通知等按需订阅

用法：
    get_sink().emit("subagent_done", payload={...}, target="feishu:oc_x")
    get_sink().subscribe(lambda rec: my_channel.send(rec.target, rec.summary))
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("agent_core")

Subscriber = Callable[["SinkRecord"], None]


@dataclass
class SinkRecord:
    kind: str
    payload: Any = None
    id: str = ""
    ts: float = 0.0
    source: str = ""
    target: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if not self.ts:
            self.ts = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "ts": self.ts, "kind": self.kind,
            "source": self.source, "target": self.target,
            "summary": self.summary, "payload": self.payload,
        }


class Sink:
    def __init__(self, path: Optional[str] = None, capacity: int = 500) -> None:
        self.path = path if path is not None else os.environ.get("NANOGHOST_SINK_PATH", "")
        self._buf: deque = deque(maxlen=capacity)
        self._subs: List[Subscriber] = []
        self._lock = threading.Lock()

    # ── 写 ────────────────────────────────────────────────
    def emit(
        self,
        kind: str,
        payload: Any = None,
        *,
        source: str = "",
        target: str = "",
        summary: str = "",
    ) -> SinkRecord:
        rec = SinkRecord(kind=kind, payload=payload, source=source, target=target, summary=summary)
        with self._lock:
            self._buf.append(rec)
            subs = list(self._subs)
        self._persist(rec)
        for fn in subs:
            try:
                fn(rec)
            except Exception:  # noqa: BLE001
                logger.exception("[Sink] subscriber failed for %s", rec.kind)
        return rec

    def _persist(self, rec: SinkRecord) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001
            logger.exception("[Sink] persist failed -> %s", self.path)

    # ── 订阅 ──────────────────────────────────────────────
    def subscribe(self, fn: Subscriber) -> None:
        with self._lock:
            self._subs.append(fn)

    def unsubscribe(self, fn: Subscriber) -> None:
        with self._lock:
            try:
                self._subs.remove(fn)
            except ValueError:
                pass

    # ── 读 ────────────────────────────────────────────────
    def recent(self, n: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._buf)[-n:]
        return [r.to_dict() for r in items]

    def count(self) -> int:
        with self._lock:
            return len(self._buf)


# ── 模块级单例 ─────────────────────────────────────────────
_SINK: Optional[Sink] = None
_LOCK = threading.Lock()


def get_sink() -> Sink:
    global _SINK
    with _LOCK:
        if _SINK is None:
            _SINK = Sink()
        return _SINK


def reset_sink() -> None:
    global _SINK
    with _LOCK:
        _SINK = None


def emit(kind: str, payload: Any = None, **kw) -> SinkRecord:
    return get_sink().emit(kind, payload, **kw)


__all__ = ["SinkRecord", "Sink", "get_sink", "reset_sink", "emit", "Subscriber"]
