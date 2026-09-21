# -*- coding: utf-8 -*-
"""P0 单测：通道抽象 + 注册表 + 端点目录 + 飞书通道适配。

只测「通用层」，不触网（用 FakeIO 替掉真实飞书 I/O）。
"""

from __future__ import annotations

import time

from agent_core.channel import (
    ChannelRegistry,
    EndpointDirectory,
    make_addr,
    parse_addr,
)
from agent_core.channel.feishu.channel import FeishuChannel


# ── 假 IO（不触网） ──────────────────────────────────────
class FakeIO:
    def __init__(self):
        self.sent = []
        self.replied = []
        self.markdown_sent = []
        self.markdown_replied = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True

    def reply(self, message_id, text):
        self.replied.append((message_id, text))
        return True

    def send_markdown(self, chat_id, text):
        self.markdown_sent.append((chat_id, text))
        return True

    def reply_markdown(self, message_id, text):
        self.markdown_replied.append((message_id, text))
        return True

    def send_images(self, chat_id, b64_list):
        return {"ok": True}

    def add_reaction(self, message_id):
        return "rid"

    def delete_reaction(self, message_id, reaction_id):
        return True


# ── 地址 ─────────────────────────────────────────────────
def test_make_and_parse_addr():
    assert make_addr("feishu", "oc_x") == "feishu:oc_x"
    assert parse_addr("feishu:oc_x") == ("feishu", "oc_x")
    assert parse_addr("oc_x") == ("", "oc_x")
    assert parse_addr("") == ("", "")


# ── 注册表 ───────────────────────────────────────────────
def test_channel_registry():
    reg = ChannelRegistry()
    ch = FeishuChannel(io=FakeIO())
    reg.register(ch)
    assert reg.get("feishu") is ch
    assert "feishu" in reg
    assert reg.names() == ["feishu"]
    assert reg.get("nope") is None
    # 重注册 → 覆盖
    ch2 = FeishuChannel(io=FakeIO())
    reg.register(ch2)
    assert reg.get("feishu") is ch2


# ── 飞书通道适配 ─────────────────────────────────────────
def test_feishu_channel_send_reply_caps():
    io = FakeIO()
    ch = FeishuChannel(io=io)
    assert ch.name == "feishu"
    assert ch.send("oc_x", "你好") is True
    assert io.sent == [("oc_x", "你好")]
    assert ch.reply("om_1", "回复") is True
    assert io.replied == [("om_1", "回复")]
    assert ch.update("om_1", "x") is False  # 暂不支持
    caps = ch.capabilities()
    assert {"text", "mention", "image"} <= caps
    assert FeishuChannel.make_addr("oc_x") == "feishu:oc_x"


def test_feishu_channel_send_blocks_uses_markdown_primitives():
    io = FakeIO()
    ch = FeishuChannel(io=io)

    ok = ch.send_blocks(
        "oc_x",
        [
            {"type": "markdown", "text": "## 标题"},
            {"type": "text", "text": "补充"},
        ],
        delivery="reply",
        reply_to="om_1",
    )

    assert ok is True
    assert io.markdown_replied == [("om_1", "## 标题")]
    assert io.sent == [("oc_x", "补充")]


# ── 端点目录 ─────────────────────────────────────────────
def test_directory_register_idempotent_and_filters():
    d = EndpointDirectory()
    ep = d.register("feishu:oc_a", channel="feishu", kind="group",
                    capabilities={"text"}, meta={"chat_name": "群A"})
    assert ep.addr == "feishu:oc_a"
    assert d.count() == 1
    t1 = ep.last_seen
    time.sleep(0.01)
    ep2 = d.register("feishu:oc_a", kind="group", meta={"chat_name": "群A改"})
    assert d.count() == 1            # 幂等：不新增
    assert ep2.last_seen >= t1       # 更新 last_seen
    assert ep2.meta["chat_name"] == "群A改"

    d.register("feishu:ou_b", channel="feishu", kind="user", meta={"name": "小王"})
    d.register("event:src1", channel="event", kind="event")
    assert len(d.list()) == 3
    assert [e.addr for e in d.list(channel="feishu")] == ["feishu:oc_a", "feishu:ou_b"]
    assert [e.addr for e in d.list(kind="user")] == ["feishu:ou_b"]
    assert [e.addr for e in d.list(q="小王")] == ["feishu:ou_b"]

    assert d.set_status("feishu:oc_a", "offline") is True
    assert d.get("feishu:oc_a").status == "offline"
    assert d.set_status("nope", "offline") is False
    assert d.unregister("event:src1") is True
    assert d.unregister("event:src1") is False
    assert d.count() == 2
    assert "feishu:oc_a" in d


# ── 模拟「收到消息即注册端点」（与 ws_client 同逻辑） ──────
def test_inbound_registers_endpoint():
    d = EndpointDirectory()
    ch = FeishuChannel(io=FakeIO())
    d.register("feishu:oc_g", channel="feishu", kind="group",
               capabilities=ch.capabilities(),
               meta={"chat_id": "oc_g", "chat_name": "工单群", "sender_id": "ou_x"})
    ep = d.get("feishu:oc_g")
    assert ep is not None
    assert ep.kind == "group"
    assert "text" in ep.capabilities
    assert ep.meta["chat_name"] == "工单群"
    assert ep.status == "online"
