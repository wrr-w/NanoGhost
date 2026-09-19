# -*- coding: utf-8 -*-
"""P3 单测：子 Agent 池（后台 / 并行 / 上限 / 排队 / 完成回报）+ 边界注入。"""

from __future__ import annotations

import re
import threading
import time

from agent_core.runtime.inbox import Inbox, InboxEvent, get_hub
from agent_core.runtime.subagent_pool import SubAgentPool


def _wait(cond, timeout=8.0, step=0.02):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return cond()


# ── 收件箱：按 kind 过滤 drain ────────────────────────────
def test_inbox_drain_kinds_filter():
    box = Inbox()
    box.put(InboxEvent(target="A", kind="a"))
    box.put(InboxEvent(target="A", kind="b"))
    box.put(InboxEvent(target="A", kind="a"))
    got = box.drain(kinds={"a"})
    assert [e.kind for e in got] == ["a", "a"]
    rest = box.drain()
    assert [e.kind for e in rest] == ["b"]  # 其余保留、保序


# ── 池：基本执行 + 运行表 + 完成事件 ─────────────────────
def test_pool_runs_and_reports():
    pool = SubAgentPool(max_concurrent=3)
    pool.start()
    try:
        rid = pool.submit("t:basic", "hello", lambda: "RESULT")
        assert _wait(lambda: (pool.get(rid) or {}).get("status") in ("done", "error"))
        rec = pool.get(rid)
        assert rec["status"] == "done" and rec["result"] == "RESULT"
        # 完成事件落到父端点收件箱
        evs = get_hub().drain("t:basic")
        assert any(e.kind == "subagent_done" and e.payload["run_id"] == rid for e in evs)
    finally:
        pool.stop()


def test_pool_supports_async_run_fn():
    pool = SubAgentPool(max_concurrent=2)
    pool.start()

    async def _afn():
        return "ASYNC"

    try:
        rid = pool.submit("t:async", "a", _afn)
        assert _wait(lambda: (pool.get(rid) or {}).get("status") in ("done", "error"))
        assert pool.get(rid)["result"] == "ASYNC"
    finally:
        pool.stop()


def test_pool_error_recorded():
    pool = SubAgentPool(max_concurrent=1)
    pool.start()
    try:
        def _boom():
            raise RuntimeError("kaboom")
        rid = pool.submit("t:err", "e", _boom)
        assert _wait(lambda: (pool.get(rid) or {}).get("status") in ("done", "error"))
        rec = pool.get(rid)
        assert rec["status"] == "error" and "kaboom" in rec["error"]
    finally:
        pool.stop()


def test_pool_concurrency_cap_and_queue():
    pool = SubAgentPool(max_concurrent=2)
    pool.start()
    lock = threading.Lock()
    st = {"cur": 0, "max": 0, "done": 0}

    def work():
        with lock:
            st["cur"] += 1
            st["max"] = max(st["max"], st["cur"])
        time.sleep(0.12)
        with lock:
            st["cur"] -= 1
            st["done"] += 1
        return "ok"

    try:
        rids = [pool.submit("t:conc", f"job{i}", work) for i in range(5)]
        assert _wait(lambda: st["done"] >= 5, timeout=10)
    finally:
        pool.stop()
    assert st["done"] == 5
    assert st["max"] <= 2                       # 上限生效
    assert all(pool.get(r)["status"] == "done" for r in rids)


# ── delegate_task：后台默认 → 立即返回 + 池执行 ────────────
class _FakeTR:
    def list_tools(self):
        return []

    def unregister(self, name):
        pass


class _FakeSub:
    def __init__(self):
        self.calls = []
        self.tool_registry = _FakeTR()

    def chat_stream_events(self, user_message, session_id=None, config=None):
        self.calls.append(user_message)

        async def _gen():
            yield ("text_stream", {"content": "..."})
            yield ("done", {"reply": f"done:{user_message}"})

        return _gen()


class _FakeAgent:
    def __init__(self):
        self.subs = {}

    def create_sub_agent(self, name):
        s = _FakeSub()
        self.subs[name] = s
        return s


def test_delegate_background_immediate_and_pooled():
    from agent_core.config import AgentConfig
    from agent_core.tool.builtins.delegate import delegate_task

    agent = _FakeAgent()
    cfg = AgentConfig(base_url="", sys_prompt="SYS", api_spec={}, verbose=False)
    ctx = {
        "agent": agent,
        "config": cfg,
        "channel_ctx": {"chat_id": "oc_dt", "platform": "feishu"},
        "delegate_depth": 0,
    }
    t0 = time.time()
    res = delegate_task({"prompt": "do thing", "description": "thing"}, ctx)
    dt = time.time() - t0
    assert res.ok, res.error
    assert dt < 1.0, f"应立返回，实际 {dt:.2f}s"
    m = re.search(r"run_id=(\w+)", res.data)
    assert m, res.data
    rid = m.group(1)

    from agent_core.runtime.subagent_pool import get_pool
    pool = get_pool()
    assert _wait(lambda: (pool.get(rid) or {}).get("status") in ("done", "error"))
    rec = pool.get(rid)
    assert rec["status"] == "done" and rec["result"] == "done:do thing"

    evs = get_hub().drain("feishu:oc_dt")
    assert any(e.kind == "subagent_done" and e.payload["run_id"] == rid for e in evs)


# ── 边界注入：drain 软事件（不动通道消息）────────────────
def test_boundary_drain_and_format():
    from agent_core.engine.agent import _drain_boundary_events, _format_boundary_events

    hub = get_hub()
    hub.submit(InboxEvent(target="feishu:ob", kind="channel_message", payload={"x": 1}))
    hub.submit(InboxEvent(target="feishu:ob", kind="subagent_done",
                          payload={"description": "巡检", "status": "done", "result": "全部正常"}))

    drained = _drain_boundary_events({"chat_id": "ob", "platform": "feishu"})
    assert len(drained) == 1 and drained[0].kind == "subagent_done"

    rest = hub.drain("feishu:ob")
    assert len(rest) == 1 and rest[0].kind == "channel_message"   # 通道消息留给下一轮

    txt = _format_boundary_events(drained)
    assert "巡检" in txt and "全部正常" in txt


def test_boundary_no_target():
    from agent_core.engine.agent import _drain_boundary_events
    assert _drain_boundary_events(None) == []
    assert _drain_boundary_events({"platform": "feishu"}) == []
