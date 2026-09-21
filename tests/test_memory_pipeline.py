# -*- coding: utf-8 -*-
"""记忆后台流水线测试。"""

from __future__ import annotations

import asyncio

from agent_core.memory.pipeline import MemoryPipeline, MemoryTurnEvent


def test_memory_pipeline_processes_event():
    seen = []

    async def handler(event: MemoryTurnEvent):
        seen.append(event.session_id)

    async def run():
        pipe = MemoryPipeline(handler=handler)
        await pipe.start()
        try:
            await pipe.submit(
                MemoryTurnEvent(
                    session_id="sess-1",
                    user_message="u",
                    reply="r",
                    all_steps_out=[],
                    step_count=1,
                )
            )
            await asyncio.sleep(0.05)
        finally:
            await pipe.stop()

    asyncio.run(run())

    assert seen == ["sess-1"]


def test_memory_pipeline_exposes_stats():
    async def handler(event: MemoryTurnEvent):
        return None

    async def run():
        pipe = MemoryPipeline(handler=handler)
        await pipe.start()
        try:
            await pipe.submit(
                MemoryTurnEvent(
                    session_id="sess-2",
                    user_message="u",
                    reply="r",
                    all_steps_out=[],
                    step_count=1,
                )
            )
            await asyncio.sleep(0.05)
            stats = pipe.stats()
            assert stats["submitted"] == 1
            assert stats["processed"] == 1
            assert stats["failed"] == 0
            assert stats["running"] is True
        finally:
            await pipe.stop()

    asyncio.run(run())
