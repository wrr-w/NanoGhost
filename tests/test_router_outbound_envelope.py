# -*- coding: utf-8 -*-
"""统一 RouteEnvelope 出站策略测试。"""

from __future__ import annotations

from agent_core.channel.base import Channel
from agent_core.channel.directory import EndpointDirectory
from agent_core.channel.endpoint import Endpoint
from agent_core.channel.manager import ChannelManager
from agent_core.channel.route import RouteEnvelope, resolve_reply_policy
from agent_core.channel.registry import ChannelRegistry
from agent_core.router import Router


def test_resolve_reply_policy_endpoint_overrides_channel_default():
    ep = Endpoint(
        addr="feishu:oc_1",
        channel="feishu",
        meta={"allow_reply": False, "prefer_reply": False},
    )
    policy = resolve_reply_policy(
        endpoint=ep,
        channel_policy={
            "supports_reply": True,
            "default_allow_reply": True,
            "default_prefer_reply": True,
        },
    )

    assert policy["supports_reply"] is True
    assert policy["allow_reply"] is False
    assert policy["prefer_reply"] is False


def test_route_envelope_outbound_defaults_are_stable():
    env = RouteEnvelope(direction="outbound", kind="text", to=["feishu:oc_1"], text="hi")

    assert env.to == ["feishu:oc_1"]
    assert env.delivery == "reply"
    assert env.text == "hi"
    assert env.reply_to is None
    assert env.images == []
    assert env.blocks == []


class FakeReplyChannel(Channel):
    name = "fake"

    def __init__(self):
        self.sent = []
        self.replied = []
        self.images = []

    def send(self, target: str, text: str) -> bool:
        self.sent.append((target, text))
        return True

    def reply(self, message_id: str, text: str) -> bool:
        self.replied.append((message_id, text))
        return True

    def default_delivery_policy(self) -> dict:
        return {
            "supports_reply": True,
            "default_allow_reply": True,
            "default_prefer_reply": True,
        }

    def send_images(self, target: str, images):
        self.images.append((target, list(images)))
        return {"ok": True}


def test_router_submit_prefers_reply_when_policy_allows():
    mgr = ChannelManager(registry=ChannelRegistry(), directory=EndpointDirectory())
    ch = FakeReplyChannel()
    mgr.register_channel(ch)
    mgr.register_endpoint("fake:a", channel="fake", kind="group", capabilities={"text"})
    router = Router(registry=mgr.registry, manager=mgr)

    rep = router.submit(RouteEnvelope(direction="outbound", kind="text", to=["fake:a"], text="hi", reply_to="m1"))

    assert rep["ok"] is True
    assert ch.replied == [("m1", "hi")]
    assert ch.sent == []


def test_router_submit_falls_back_to_send_when_endpoint_disables_reply():
    mgr = ChannelManager(registry=ChannelRegistry(), directory=EndpointDirectory())
    ch = FakeReplyChannel()
    mgr.register_channel(ch)
    mgr.register_endpoint("fake:b", channel="fake", kind="group", capabilities={"text"}, meta={"allow_reply": False})
    router = Router(registry=mgr.registry, manager=mgr)

    rep = router.submit(RouteEnvelope(direction="outbound", kind="text", to=["fake:b"], text="hi", reply_to="m2"))

    assert rep["ok"] is True
    assert ch.replied == []
    assert ch.sent == [("b", "hi")]


def test_router_submit_routes_images_via_channel_extension():
    mgr = ChannelManager(registry=ChannelRegistry(), directory=EndpointDirectory())
    ch = FakeReplyChannel()
    mgr.register_channel(ch)
    mgr.register_endpoint("fake:c", channel="fake", kind="group", capabilities={"text"})
    router = Router(registry=mgr.registry, manager=mgr)

    rep = router.submit(RouteEnvelope(direction="outbound", kind="image", to=["fake:c"], images=["data:image/png;base64,abc"]))

    assert rep["ok"] is True
    assert ch.images == [("c", ["data:image/png;base64,abc"])]


def test_router_submit_routes_blocks_in_order():
    mgr = ChannelManager(registry=ChannelRegistry(), directory=EndpointDirectory())
    ch = FakeReplyChannel()
    mgr.register_channel(ch)
    mgr.register_endpoint("fake:d", channel="fake", kind="group", capabilities={"text"})
    router = Router(registry=mgr.registry, manager=mgr)

    rep = router.submit(
        RouteEnvelope(
            direction="outbound",
            kind="message",
            delivery="reply",
            to=["fake:d"],
            reply_to="m3",
            blocks=[
                {"type": "text", "text": "head"},
                {"type": "image", "images": ["data:image/png;base64,abc"]},
                {"type": "markdown", "text": "tail"},
            ],
        )
    )

    assert rep["ok"] is True
    assert ch.replied == [("m3", "head")]
    assert ch.images == [("d", ["data:image/png;base64,abc"])]
    assert ch.sent == [("d", "tail")]
