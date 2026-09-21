# -*- coding: utf-8 -*-
"""Presenter 边界测试：当前会话直出 vs 主动路由。"""

from __future__ import annotations

from agent_core.channel.route import RouteEnvelope
from agent_core.channel.responder import TurnResponder


class FakeRouter:
    def __init__(self):
        self.envelopes = []

    def submit(self, envelope, *, hop=0):
        self.envelopes.append((envelope, hop))
        return {"ok": True, "sent": list(envelope.to or [])}


def test_turn_responder_reply_current_creates_outbound_route_envelope():
    router = FakeRouter()
    responder = TurnResponder(router=router, platform="feishu", chat_id="oc_x", message_id="om_1")

    responder.reply_current("done")

    env, hop = router.envelopes[0]
    assert hop == 0
    assert isinstance(env, RouteEnvelope)
    assert env.direction == "outbound"
    assert env.to == ["feishu:oc_x"]
    assert env.text == "done"
    assert env.reply_to == "om_1"


def test_turn_responder_send_current_creates_plain_envelope():
    router = FakeRouter()
    responder = TurnResponder(router=router, platform="feishu", chat_id="oc_x")

    responder.send_current("done")

    env, _ = router.envelopes[0]
    assert env.to == ["feishu:oc_x"]
    assert env.text == "done"
    assert env.reply_to is None


def test_turn_responder_notify_uses_plain_envelope():
    router = FakeRouter()
    responder = TurnResponder(router=router, platform="feishu", chat_id="oc_x", message_id="om_1", namespace="ns")

    report = responder.notify(["feishu:ou_a"], "hello")

    assert report["ok"] is True
    env, _ = router.envelopes[0]
    assert env.to == ["feishu:ou_a"]
    assert env.reply_to is None
    assert env.agent_key == "ns"


def test_turn_responder_send_images_creates_image_envelope():
    router = FakeRouter()
    responder = TurnResponder(router=router, platform="feishu", chat_id="oc_x")

    responder.send_images(["data:image/png;base64,abc"])

    env, _ = router.envelopes[0]
    assert env.to == ["feishu:oc_x"]
    assert env.images == ["data:image/png;base64,abc"]
