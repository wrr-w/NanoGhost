# -*- coding: utf-8 -*-
"""Presenter 边界测试：统一发送原语 emit（blocks → 唯一出口）。"""

from __future__ import annotations

from agent_core.channel.route import (
    RouteEnvelope,
    make_image_block,
    make_markdown_block,
    make_text_block,
)
from agent_core.channel.responder import TurnResponder


class FakeRouter:
    def __init__(self):
        self.envelopes = []

    def submit(self, envelope, *, hop=0):
        self.envelopes.append((envelope, hop))
        return {"ok": True, "sent": list(envelope.to or [])}


def test_emit_reply_defaults_to_source_and_reply():
    router = FakeRouter()
    r = TurnResponder(router=router, platform="feishu", chat_id="oc_x", message_id="om_1")

    r.emit([make_markdown_block("done")])

    env, hop = router.envelopes[0]
    assert hop == 0
    assert isinstance(env, RouteEnvelope)
    assert env.direction == "outbound"
    assert env.delivery == "reply"
    assert env.to == ["feishu:oc_x"]
    assert env.reply_to == "om_1"
    assert env.blocks[0]["type"] == "markdown"
    assert env.blocks[0]["text"] == "done"


def test_emit_send_text_block_is_plain():
    router = FakeRouter()
    r = TurnResponder(router=router, platform="feishu", chat_id="oc_x")

    r.emit([make_text_block("hi")], delivery="send")

    env, _ = router.envelopes[0]
    assert env.delivery == "send"
    assert env.to == ["feishu:oc_x"]
    assert env.reply_to is None            # send 不回消息
    assert env.blocks[0]["type"] == "text"
    assert env.blocks[0]["text"] == "hi"


def test_emit_image_block():
    router = FakeRouter()
    r = TurnResponder(router=router, platform="feishu", chat_id="oc_x")

    r.emit([make_image_block(["data:image/png;base64,abc"])], delivery="send")

    env, _ = router.envelopes[0]
    assert env.kind == "image"
    assert env.images == ["data:image/png;base64,abc"]
    assert env.blocks[0]["type"] == "image"


def test_emit_targets_override():
    router = FakeRouter()
    r = TurnResponder(router=router, platform="feishu", chat_id="oc_x", message_id="om_1")

    r.emit([make_markdown_block("x")], targets=["feishu:oc_other"])

    env, _ = router.envelopes[0]
    assert env.to == ["feishu:oc_other"]
    assert env.target_addr == "feishu:oc_other"


def test_emit_empty_blocks_is_noop():
    router = FakeRouter()
    r = TurnResponder(router=router, platform="feishu", chat_id="oc_x")

    rep = r.emit([])

    assert rep["ok"] is False
    assert router.envelopes == []
