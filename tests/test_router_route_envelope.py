# -*- coding: utf-8 -*-
"""统一 RouteEnvelope 与 Router.submit 测试。"""

from __future__ import annotations

from agent_core.channel.route import RouteEnvelope
from agent_core.router import Router
from agent_core.runtime.inbox import InboxHub


def test_route_envelope_defaults_are_stable():
    env = RouteEnvelope(direction="inbound", kind="channel_message", target_addr="feishu:oc_x")

    assert env.direction == "inbound"
    assert env.kind == "channel_message"
    assert env.target_addr == "feishu:oc_x"
    assert env.delivery == "reply"
    assert env.to == []
    assert env.images == []
    assert env.blocks == []
    assert env.meta == {}


def test_router_submit_rejects_unknown_direction():
    router = Router()

    rep = router.submit(RouteEnvelope(direction="sideways", kind="event", target_addr="x"))

    assert rep["ok"] is False
    assert "direction" in rep["error"]


def test_router_submit_inbound_adapts_to_inbox_event():
    hub = InboxHub()
    router = Router(hub=hub)

    router.submit(
        RouteEnvelope(
            direction="inbound",
            kind="timer",
            target_addr="feishu:sched:daily",
            source_addr="timer",
            payload={"task": "daily"},
            summary="定时任务 daily",
        )
    )

    ev = hub.drain("feishu:sched:daily")[0]
    assert ev.kind == "timer"
    assert ev.source == "timer"
    assert ev.summary == "定时任务 daily"
