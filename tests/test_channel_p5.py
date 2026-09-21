# -*- coding: utf-8 -*-
"""P5 单测：ChannelManager 控制面 + Router 端点守卫。"""

from __future__ import annotations

from agent_core.channel.admin import channel_overview
from agent_core.channel.base import Channel
from agent_core.channel.manager import ChannelManager
from agent_core.channel.registry import ChannelRegistry
from agent_core.channel.directory import EndpointDirectory
from agent_core.router import Router


class FakeChannel(Channel):
    name = "fake"

    def __init__(self):
        self.calls = []

    def send(self, target: str, text: str) -> bool:
        self.calls.append((target, text))
        return True

    def reply(self, message_id: str, text: str) -> bool:
        return True

    def capabilities(self):
        return {"text"}


def _make_manager() -> ChannelManager:
    return ChannelManager(registry=ChannelRegistry(), directory=EndpointDirectory())


def test_router_skips_disabled_endpoint():
    mgr = _make_manager()
    mgr.register_channel(FakeChannel())
    mgr.register_endpoint("fake:a", channel="fake", kind="group", capabilities={"text"})
    mgr.enable_endpoint("fake:a", False)

    rep = Router(registry=mgr.registry, manager=mgr).deliver(["fake:a"], "hi")

    assert rep["sent"] == []
    assert rep["skipped"] == ["fake:a"]
    assert rep["reasons"]["fake:a"] == "endpoint_disabled"


def test_router_skips_endpoint_without_text_capability():
    mgr = _make_manager()
    mgr.register_channel(FakeChannel())
    mgr.register_endpoint("fake:b", channel="fake", kind="group", capabilities={"image"})

    rep = Router(registry=mgr.registry, manager=mgr).deliver(["fake:b"], "hi")

    assert rep["sent"] == []
    assert rep["skipped"] == ["fake:b"]
    assert rep["reasons"]["fake:b"] == "missing_capability:text"


def test_router_skips_disabled_channel():
    mgr = _make_manager()
    mgr.register_channel(FakeChannel())
    mgr.register_endpoint("fake:c", channel="fake", kind="group", capabilities={"text"})
    mgr.enable_channel("fake", False)

    rep = Router(registry=mgr.registry, manager=mgr).deliver(["fake:c"], "hi")

    assert rep["sent"] == []
    assert rep["skipped"] == ["fake:c"]
    assert rep["reasons"]["fake:c"] == "channel_disabled"


def test_channel_overview_includes_enabled_flag():
    mgr = _make_manager()
    mgr.register_channel(FakeChannel())
    mgr.register_endpoint("fake:d", channel="fake", kind="group", capabilities={"text"})
    mgr.enable_channel("fake", False)

    rows = channel_overview(manager=mgr)

    assert rows[0]["channel"] == "fake"
    assert rows[0]["enabled"] is False
    assert rows[0]["endpoints"] == 1
