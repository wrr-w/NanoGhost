from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("agent_core")


@dataclass
class MemoryTurnEvent:
    session_id: Optional[str]
    user_message: str
    reply: str
    all_steps_out: list[dict[str, Any]]
    step_count: int


class MemoryPipeline:
    def __init__(self, handler: Callable[[MemoryTurnEvent], Awaitable[None]]) -> None:
        self._handler = handler
        self._q: asyncio.Queue[MemoryTurnEvent | None] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._stats = {"submitted": 0, "processed": 0, "failed": 0}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="memory-pipeline")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        await self._q.put(None)
        if self._task:
            await self._task
            self._task = None

    async def submit(self, event: MemoryTurnEvent) -> None:
        self._stats["submitted"] += 1
        await self._q.put(event)

    def stats(self) -> dict[str, Any]:
        return {
            "submitted": self._stats["submitted"],
            "processed": self._stats["processed"],
            "failed": self._stats["failed"],
            "queue_size": self._q.qsize(),
            "running": self._running,
        }

    async def _run(self) -> None:
        while True:
            event = await self._q.get()
            if event is None:
                break
            try:
                await self._handler(event)
                self._stats["processed"] += 1
            except Exception:  # noqa: BLE001
                self._stats["failed"] += 1
                logger.exception("[MemoryPipeline] event failed session=%s", event.session_id)


__all__ = ["MemoryPipeline", "MemoryTurnEvent"]
