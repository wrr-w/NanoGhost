from __future__ import annotations

import asyncio
import inspect
import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("agent_core")


@dataclass
class MemoryTurnEvent:
    session_id: Optional[str]
    user_message: str
    reply: str
    all_steps_out: list[dict[str, Any]]
    step_count: int


class MemoryPipeline:
    """回合后处理队列（Card / Graph / memory.md / daily）。

    为什么是**常驻守护线程**而不是 asyncio 任务：
        通道 worker 是短生命周期的事件循环，`asyncio.create_task` 排的
        fire-and-forget 任务会随该循环一起被回收 —— 实测 `postprocess_turn`
        因此**从未执行过**，这正是 `agent_memory_cards` / `agent_edges_ml`
        长期为 0 行的根因。线程与进程同寿，完全不依赖任何事件循环，
        所以在任何宿主（网关 worker / CLI / 测试）里都必然会被消费。

    handler 可以是同步或异步可调用；异步 handler 在本线程的私有事件循环上跑。
    """

    def __init__(self, handler: Callable[[MemoryTurnEvent], Any]) -> None:
        self._handler = handler
        self._q: "queue.Queue[Optional[MemoryTurnEvent]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._lock = threading.Lock()
        self._stats = {"submitted": 0, "processed": 0, "failed": 0}

    # ---- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._run, name="memory-pipeline", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    # 兼容早期 async 调用点（agent 侧已改同步，这里保留别名不破坏外部调用）
    async def astart(self) -> None:  # pragma: no cover - 兼容层
        self.start()

    async def astop(self, timeout: float = 5.0) -> None:  # pragma: no cover - 兼容层
        self.stop(timeout)

    # ---- 投递 / 观测 ------------------------------------------------------

    def submit(self, event: MemoryTurnEvent) -> None:
        self._stats["submitted"] += 1
        self._q.put(event)

    async def asubmit(self, event: MemoryTurnEvent) -> None:  # pragma: no cover - 兼容层
        self.submit(event)

    def stats(self) -> dict[str, Any]:
        return {
            "submitted": self._stats["submitted"],
            "processed": self._stats["processed"],
            "failed": self._stats["failed"],
            "queue_size": self._q.qsize(),
            "running": self._running,
        }

    # ---- 消费者 -----------------------------------------------------------

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            while True:
                event = self._q.get()
                if event is None:
                    break
                try:
                    result = self._handler(event)
                    if inspect.isawaitable(result):
                        loop.run_until_complete(result)
                    self._stats["processed"] += 1
                except Exception:  # noqa: BLE001
                    self._stats["failed"] += 1
                    logger.exception(
                        "[MemoryPipeline] event failed session=%s", event.session_id
                    )
        finally:
            self._loop = None
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["MemoryPipeline", "MemoryTurnEvent"]
