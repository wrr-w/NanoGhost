# -*- coding: utf-8 -*-
"""飞书渠道 - 三层架构。

通用层（channel/ 下）：
  message_context.py   — 标准：数据类型 + 渲染器
  identity.py          — Bot 身份、Prompts、Memory
  session.py           — Session 管理器

飞书特有（feishu/ 下）：
  instance.py          — SDK 长连接管理
  turn.py              — 飞书事件 → MessageSource + MessageContext
  ws_client.py         — 编排层
"""

from .api import (
    get_tenant_access_token,
    send_text_message_to_chat,
    send_images_base64_to_chat,
    download_message_resource,
    extract_text_from_event_message,
    extract_image_keys_from_event_message,
    extract_file_info_from_event_message,
)
from .sdk import FeishuSDK
from .turn import FeishuTurnParser
from .ws_client import FeishuWSClient

__all__ = [
    "FeishuWSClient",
    "FeishuSDK",
    "FeishuTurnParser",
    "get_tenant_access_token",
    "send_text_message_to_chat",
    "send_images_base64_to_chat",
    "download_message_resource",
    "extract_text_from_event_message",
    "extract_image_keys_from_event_message",
    "extract_file_info_from_event_message",
]
