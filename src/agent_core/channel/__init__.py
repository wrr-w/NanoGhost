# -*- coding: utf-8 -*-
"""可插拔的 Agent 通道。

通用层（渠道无关）：
  message_context.py  — 标准数据类型 + ContextBuilder
  identity.py         — Bot 身份、Prompts、Memory
  session.py          — Session 管理器

渠道适配（渠道特有）：
  feishu/             — 飞书
  telegram/           — （预留）
"""

from .instance import BotInstance
from .session import SessionStore
from .message_context import (
    MessageType, ChatType, MentionRef,
    MessageSource, MessageContext, ContextBuilder,
)

# P0：通道抽象 + 注册表 + 端点目录
from .base import Channel
from .registry import (
    ChannelRegistry, REGISTRY,
    get_registry, register_channel, get_channel,
)
from .endpoint import Endpoint, make_addr, parse_addr
from .directory import (
    EndpointDirectory, DIRECTORY,
    get_directory, register_endpoint, list_endpoints,
)

__all__ = [
    "BotInstance",
    "SessionStore",
    "MessageType", "ChatType",
    "MentionRef", "MessageSource", "MessageContext",
    "ContextBuilder",
    # P0
    "Channel",
    "ChannelRegistry", "REGISTRY", "get_registry", "register_channel", "get_channel",
    "Endpoint", "make_addr", "parse_addr",
    "EndpointDirectory", "DIRECTORY", "get_directory", "register_endpoint", "list_endpoints",
]
