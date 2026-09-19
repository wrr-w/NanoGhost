# -*- coding: utf-8 -*-
"""常驻消费者 —— P1。

职责只有一个：盯着收件箱，**按端点忙闲**把事件交给 handler。
  · 端点闲 → 起一个 worker（独立线程 + 独立 event loop）处理
  · 端点忙 → 不动（事件留在队列；该端点当前 worker 跑完会再捞）
  · 不同端点 → 并发（线程池）

与渠道 WS 并列运行（ws_client.run_forever 里 start）。不引外部 broker。

为什么用「线程池 + asyncio.run」而不是单 loop：
  agent 那一轮里有阻塞 I/O（DB / MCP / HTTP）——单 loop 会把不同端点串行化。
  沿用代码既有「一条消息 = 一个线程 + 一个 loop」的模型，才能保证
  **端点内串行、端点间并发**。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Awaitable, Callable, List, Optional, Set

from .inbox import InboxEvent, InboxHub, get_hub

logger = logging.getLogger("agent_core")

#: handler(target, events) -> awaitable；events 是「该端点这一批」的事件
Handler = Callable[[str, List[InboxEvent]], Awaitable[None]]


class ResidentConsumer:
    """常驻消费者：看队列 → 判忙闲 → 派 worker。"""

    def __init__(
        self,
        hub: Optional[InboxHub] = None,
        handler: Optional[Handler] = None,
        *,
        max_workers: int = 8,
        poll_interval: float = 1.0,
    ) -> None:
        self.hub = hub or get_hub()
        self.handler = handler
        self.max_workers = max_workers
        self.poll_interval = poll_interval
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="consumer")
        self._busy: Set[str] = set()
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── 生产（外部可调；也可直接用 hub.submit）──
    def submit(self, target: str, event: InboxEvent) -> InboxEvent:
        return self.hub.submit(event)

    # ── 生命周期 ──
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="resident-consumer", daemon=True)
        self._thread.start()
        logger.info("[Consumer] started (max_workers=%s)", self.max_workers)

    def stop(self) -> None:
        self._running = False
        try:
            self._pool.shutdown(wait=False)
        except Exception:
            pass
        logger.info("[Consumer] stopped")

    # ── 消费者主循环（自己的线程；只做「看 + 派活」，不干重活）──
    def _loop(self) -> None:
        while self._running:
            try:
                self.hub.wait(self.poll_interval)
                for target in self.hub.targets():
                    if self.hub.size(target) == 0:
                        continue
                    with self._lock:
                        if target in self._busy:
                            continue
                        self._busy.add(target)
                    try:
                        self._pool.submit(self._work, target)
                    except Exception:
                        with self._lock:
                            self._busy.discard(target)
                        logger.exception("[Consumer] submit worker failed target=%s", target)
            except Exception:
                logger.exception("[Consumer] loop error")
                time.sleep(self.poll_interval)

    # ── worker：真正调 handler（独立线程 + 独立 loop）──
    def _work(self, target: str) -> None:
        try:
            while True:
                batch = self.hub.drain(target)
                if not batch:
                    break
                logger.info("[Consumer] ▶ target=%s 处理 %d 条事件", target, len(batch))
                try:
                    asyncio.run(self._dispatch(target, batch))
                except Exception:
                    logger.exception("[Consumer] handler failed target=%s", target)
        finally:
            with self._lock:
                self._busy.discard(target)
            # 竞态兜底：drain 之后、清 busy 之前，若又有新事件进来 → 再唤醒一次
            try:
                if self.hub.size(target) > 0:
                    self.hub.wake()
            except Exception:
                pass

    async def _dispatch(self, target: str, batch: List[InboxEvent]) -> None:
        if self.handler is None:
            logger.warning("[Consumer] no handler set; %d events dropped (target=%s)", len(batch), target)
            return
        await self.handler(target, batch)

    # ── 观测 ──
    def is_busy(self, target: str) -> bool:
        with self._lock:
            return target in self._busy

    def busy_targets(self) -> Set[str]:
        with self._lock:
            return set(self._busy)


__all__ = ["ResidentConsumer", "Handler"]
