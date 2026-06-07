# -*- coding: utf-8 -*-
"""飞书 ChannelIO 实现。

将抽象 I/O 接口映射到飞书具体 API。
支持 @昵称 反查为 <at user_id="ou_xxx">@昵称</at> 以触发真正的飞书 @通知。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from agent_core.channel.interfaces import ChannelIO
from . import api

logger = logging.getLogger("agent_core")


class FeishuIO(ChannelIO):
    """飞书渠道 I/O 实现。"""

    def __init__(self):
        self._name_map: Dict[str, str] = {}

    def set_name_map(self, name_map: Dict[str, str]) -> None:
        """设置 name -> user_id 映射（引用，外部更新后自动生效）。"""
        self._name_map = name_map

    def _replace_at_mentions(self, text: str) -> str:
        """把 @昵称 替换为飞书 <at> 标签。

        按名字长度降序替换，避免短名匹配到长名的一部分。
        interactive 卡片不支持 <at>，检测到 <at> 时 reply 会降级为纯文本。
        """
        if not self._name_map or not text:
            return text
        for name, uid in sorted(self._name_map.items(), key=lambda x: -len(x[0])):
            text = text.replace(f"@{name}", f'<at user_id="{uid}">@{name}</at>')
        return text

    def send_text(self, chat_id: str, text: str) -> bool:
        text = self._replace_at_mentions(text)
        return api.send_text_message_to_chat(chat_id, text)

    def reply(self, message_id: str, text: str) -> bool:
        text = self._replace_at_mentions(text)
        # interactive 卡片不支持 <at> 标签，若替换后有 at 则降级为纯文本回复
        if '<at user_id=' in text:
            return api.reply_text_to_message(message_id, text)
        return api.reply_to_message(message_id, text)

    def send_images(self, chat_id: str, b64_list: List[str]) -> Dict[str, Any]:
        return api.send_images_base64_to_chat(chat_id, b64_list)

    def add_reaction(self, message_id: str) -> str:
        return api.add_reaction_to_message(message_id)

    def delete_reaction(self, message_id: str, reaction_id: str) -> bool:
        return api.delete_reaction_to_message(message_id, reaction_id)

    def download_image(self, message_id: str, file_key: str) -> Optional[Tuple[bytes, str]]:
        return api.download_message_resource(message_id, file_key, resource_type="image")
