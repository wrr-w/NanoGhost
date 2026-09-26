# -*- coding: utf-8 -*-
"""记忆后台流水线测试。

重点覆盖 A0 的回归：后处理必须**不依赖事件循环**也能被消费 ——
旧实现是 asyncio 任务，所以「同步投递、无线程池、无事件循环」这条路径
必须显式测到。
"""

from __future__ import annotations

import asyncio
import time

from agent_core.memory.pipeline import MemoryPipeline, MemoryTurnEvent


def _event(sid: str) -> MemoryTurnEvent:
    return MemoryTurnEvent(
        session_id=sid, user_message="u", reply="r", all_steps_out=[], step_count=1
    )


def test_memory_pipeline_processes_without_event_loop():
    """无事件循环、同步 submit 也要被处理（A0 的核心回归）。"""
    seen = []
    pipe = MemoryPipeline(handler=lambda e: seen.append(e.session_id))
    pipe.start()
    try:
        pipe.submit(_event("sess-sync"))
        deadline = time.time() + 3.0
        while time.time() < deadline and not seen:
            time.sleep(0.02)
    finally:
        pipe.stop()
    assert seen == ["sess-sync"]


def test_memory_pipeline_supports_async_handler():
    seen = []

    async def handler(event: MemoryTurnEvent):
        seen.append(event.session_id)

    pipe = MemoryPipeline(handler=handler)
    pipe.start()
    try:
        pipe.submit(_event("sess-async"))
        deadline = time.time() + 3.0
        while time.time() < deadline and not seen:
            time.sleep(0.02)
    finally:
        pipe.stop()
    assert seen == ["sess-async"]


def test_memory_pipeline_exposes_stats():
    pipe = MemoryPipeline(handler=lambda e: None)
    pipe.start()
    try:
        pipe.submit(_event("sess-stats"))
        deadline = time.time() + 3.0
        while time.time() < deadline and pipe.stats()["processed"] < 1:
            time.sleep(0.02)
        stats = pipe.stats()
        assert stats["submitted"] == 1
        assert stats["processed"] == 1
        assert stats["failed"] == 0
        assert stats["running"] is True
    finally:
        pipe.stop()


def test_memory_pipeline_counts_failures():
    def boom(event):
        raise RuntimeError("boom")

    pipe = MemoryPipeline(handler=boom)
    pipe.start()
    try:
        pipe.submit(_event("sess-fail"))
        deadline = time.time() + 3.0
        while time.time() < deadline and pipe.stats()["failed"] < 1:
            time.sleep(0.02)
        assert pipe.stats()["failed"] == 1
        assert pipe.stats()["processed"] == 0
    finally:
        pipe.stop()


def test_memory_pipeline_keeps_async_api_compatible():
    """旧的 await 调用点仍可用（start/submit/stop 的 async 别名）。"""
    seen = []

    async def run():
        pipe = MemoryPipeline(handler=lambda e: seen.append(e.session_id))
        await pipe.astart()
        try:
            await pipe.asubmit(_event("sess-compat"))
            await asyncio.sleep(0.1)
        finally:
            await pipe.astop()

    asyncio.run(run())
    assert seen == ["sess-compat"]
