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

__all__ = [
    "BotInstance",
    "SessionStore",
    "MessageType", "ChatType",
    "MentionRef", "MessageSource", "MessageContext",
    "ContextBuilder",
]
