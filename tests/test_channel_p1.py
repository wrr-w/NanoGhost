# -*- coding: utf-8 -*-
"""P1 单测：每端点队列（收件箱）+ 常驻消费者。

只测「通用层」，不碰飞书 / agent。
"""

from __future__ import annotations

import asyncio
import threading
import time

from agent_core.runtime import InboxEvent, InboxHub, ResidentConsumer
from agent_core.runtime.inbox import Inbox


# ── 收件箱 ───────────────────────────────────────────────
def test_inbox_event_defaults():
    ev = InboxEvent(target="feishu:oc_x", kind="channel_message", payload={"a": 1})
    assert ev.id and len(ev.id) >= 8
    assert ev.ts > 0
    assert ev.to_dict()["target"] == "feishu:oc_x"
    assert ev.to_dict()["kind"] == "channel_message"


def test_inbox_fifo_and_drain():
    box = Inbox()
    box.put(InboxEvent(target="A", payload=1))
    box.put(InboxEvent(target="A", payload=2))
    assert box.size() == 2
    items = box.drain()
    assert [e.payload for e in items] == [1, 2]  # FIFO
    assert box.size() == 0
    assert box.drain() == []


def test_hub_submit_size_targets_total():
    h = InboxHub()
    assert h.total() == 0
    h.submit(InboxEvent(target="A", kind="m", payload=1))
    h.submit(InboxEvent(target="A", kind="m", payload=2))
    h.submit(InboxEvent(target="B", kind="m"))
    assert h.size("A") == 2
    assert h.size("B") == 1
    assert h.total() == 3
    assert h.snapshot() == {"A": 2, "B": 1}
    assert set(h.targets()) == {"A", "B"}
    batch = h.drain("A")
    assert [e.payload for e in batch] == [1, 2]
    assert h.size("A") == 0
    # wait 不阻塞太久
    t0 = time.time()
    h.wait(0.05)
    assert time.time() - t0 < 1.0


def test_hub_submit_requires_target():
    h = InboxHub()
    try:
        h.submit(InboxEvent(target=""))
        assert False, "should raise"
    except ValueError:
        pass


# ── 常驻消费者 ───────────────────────────────────────────
def _wait_until(cond, timeout=5.0, step=0.02):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return cond()


def test_consumer_processes_all_events():
    hub = InboxHub()
    got = []
    lock = threading.Lock()

    async def handler(target, events):
        with lock:
            got.append((target, [e.payload for e in events]))

    c = ResidentConsumer(hub, handler, max_workers=4)
    c.start()
    try:
        for i in range(3):
            hub.submit(InboxEvent(target="A", kind="m", payload=i))
        hub.submit(InboxEvent(target="B", kind="m", payload=99))
        ok = _wait_until(lambda: sum(len(p) for _, p in got) >= 4)
        assert ok, f"未处理完: {got}"
    finally:
        c.stop()

    a_payloads = sorted(p for t, ps in got if t == "A" for p in ps)
    b_payloads = [p for t, ps in got if t == "B" for p in ps]
    assert a_payloads == [0, 1, 2]
    assert b_payloads == [99]


def test_consumer_per_target_serial():
    """同一端点内串行：并发度恒为 1。"""
    hub = InboxHub()
    per = {}
    max_per = {}
    lock = threading.Lock()

    async def handler(target, events):
        with lock:
            per[target] = per.get(target, 0) + 1
            max_per[target] = max(max_per.get(target, 0), per[target])
        await asyncio.sleep(0.05)
        with lock:
            per[target] -= 1

    c = ResidentConsumer(hub, handler, max_workers=4)
    c.start()
    try:
        for i in range(4):
            hub.submit(InboxEvent(target="A", kind="m", payload=i))
        _wait_until(lambda: max_per.get("A", 0) >= 1)
        time.sleep(0.4)  # 让 A 的 4 条都跑完
    finally:
        c.stop()

    assert max_per.get("A", 0) == 1  # A 内绝不并发


def test_consumer_multiple_targets_concurrent():
    """不同端点都会被处理（且可并发）。"""
    hub = InboxHub()
    got = set()
    lock = threading.Lock()

    async def handler(target, events):
        await asyncio.sleep(0.02)
        with lock:
            got.add(target)

    c = ResidentConsumer(hub, handler, max_workers=4)
    c.start()
    try:
        hub.submit(InboxEvent(target="A", kind="m"))
        hub.submit(InboxEvent(target="B", kind="m"))
        hub.submit(InboxEvent(target="C", kind="m"))
        ok = _wait_until(lambda: got >= {"A", "B", "C"})
        assert ok, f"未处理: {got}"
    finally:
        c.stop()


def test_consumer_busy_tracking_and_no_handler():
    hub = InboxHub()
    c = ResidentConsumer(hub, None, max_workers=2)
    # 没 handler 也不能崩
    c.start()
    try:
        hub.submit(InboxEvent(target="X", kind="m"))
        time.sleep(0.2)
        assert c.busy_targets() <= {"X"}
    finally:
        c.stop()
    assert c.is_busy("X") is False
