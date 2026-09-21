# -*- coding: utf-8 -*-
"""飞书 markdown I/O 行为：原生发送 + mention 降级。"""

from __future__ import annotations

from agent_core.channel.feishu.io import FeishuIO


def test_send_markdown_uses_markdown_api_without_mentions(monkeypatch):
    io = FeishuIO()
    calls = []

    monkeypatch.setattr(
        "agent_core.channel.feishu.api.send_markdown_message_to_chat",
        lambda chat_id, text: calls.append(("markdown", chat_id, text)) or True,
    )
    monkeypatch.setattr(
        "agent_core.channel.feishu.api.send_text_message_to_chat",
        lambda chat_id, text: calls.append(("text", chat_id, text)) or True,
    )

    assert io.send_markdown("oc_x", "## 标题") is True
    assert calls == [("markdown", "oc_x", "## 标题")]


def test_send_markdown_falls_back_to_text_when_mentions_present(monkeypatch):
    io = FeishuIO()
    io.set_name_map({"张三": "ou_1"})
    calls = []

    monkeypatch.setattr(
        "agent_core.channel.feishu.api.send_markdown_message_to_chat",
        lambda chat_id, text: calls.append(("markdown", chat_id, text)) or True,
    )
    monkeypatch.setattr(
        "agent_core.channel.feishu.api.send_text_message_to_chat",
        lambda chat_id, text: calls.append(("text", chat_id, text)) or True,
    )

    assert io.send_markdown("oc_x", "@张三 请处理") is True
    assert calls == [("text", "oc_x", '<at user_id="ou_1">@张三</at> 请处理')]


def test_reply_markdown_falls_back_to_text_reply_when_mentions_present(monkeypatch):
    io = FeishuIO()
    io.set_name_map({"张三": "ou_1"})
    calls = []

    monkeypatch.setattr(
        "agent_core.channel.feishu.api.reply_to_message",
        lambda message_id, text: calls.append(("markdown", message_id, text)) or True,
    )
    monkeypatch.setattr(
        "agent_core.channel.feishu.api.reply_text_to_message",
        lambda message_id, text: calls.append(("text", message_id, text)) or True,
    )

    assert io.reply_markdown("om_1", "@张三 看下") is True
    assert calls == [("text", "om_1", '<at user_id="ou_1">@张三</at> 看下')]
