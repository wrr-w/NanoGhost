# -*- coding: utf-8 -*-
"""FeishuChannel 作为「完整适配器」的契约（乙b · 组合）。"""

from __future__ import annotations

from agent_core.channel.base import Channel
from agent_core.channel.feishu.channel import FeishuChannel
from agent_core.channel.feishu.transport import FeishuTransport
from agent_core.channel.feishu.ws_client import FeishuWSClient


class _FakeIO:
    def send_text(self, target, text):
        return True

    def send_markdown(self, target, text):
        return True


def test_ws_client_is_alias_of_channel():
    """旧名 FeishuWSClient 只是 FeishuChannel 的别名（兼容层）。"""
    assert FeishuWSClient is FeishuChannel


def test_channel_declares_inbound_contract():
    """Channel 基类新增入站归一契约 parse_inbound。"""
    assert hasattr(Channel, "parse_inbound")
    assert issubclass(FeishuChannel, Channel)
    ch = FeishuChannel(io=_FakeIO())
    assert callable(ch.parse_inbound)


def test_pure_io_channel_has_no_transport():
    """纯出站场景（无 agent）：不组装接收管道/会话，仅 io + 归一器。"""
    ch = FeishuChannel(io=_FakeIO())
    assert ch.transport is None
    assert ch.agent is None
    assert ch.sessions is None
    assert ch.turn is not None          # 归一器始终可用


def test_transport_class_exposed():
    assert FeishuTransport.__name__ == "FeishuTransport"
