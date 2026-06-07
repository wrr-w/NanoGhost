# -*- coding: utf-8 -*-
"""
轮次层：单条飞书消息的解析和归一化。

把飞书 SDK 原始事件翻译成 MessageSource + MessageContext，
供 ContextBuilder 格式化为 LLM 可读的文本。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from agent_core.channel.message_context import (
    ContextBuilder, MessageSource, MessageContext, MentionRef,
)
from .api import (
    download_message_resource,
    extract_file_info_from_event_message,
    extract_image_keys_from_event_message,
    extract_text_from_event_message,
    get_message_by_id,
    get_user_name,
    parse_message_content,
)

logger = logging.getLogger("agent_core")

# 可解码为文本的文件扩展名
_TEXT_EXTS = {
    "md", "txt", "py", "json", "yaml", "yml",
    "toml", "ini", "cfg", "conf", "log", "csv",
    "xml", "html", "css", "js", "ts", "sh", "bat",
    "ps1", "sql", "r", "go", "rs", "java", "c",
    "cpp", "h", "hpp", "lua", "rb", "php",
}


class FeishuTurnParser:
    """将飞书事件解析为归一化的 MessageSource + MessageContext。"""

    def __init__(self, context_builder: ContextBuilder):
        self._context_builder = context_builder

    def parse_event(self, event_data: dict) -> Tuple[MessageSource, MessageContext]:
        """解析飞书事件，返回 (MessageSource, MessageContext)。"""
        event = event_data.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {}) or {}

        # ── 基础字段 ──
        chat_id = (message.get("chat_id") or "").strip()
        chat_type = (message.get("chat_type") or "").strip()
        message_id = (message.get("message_id") or message.get("id") or "").strip()
        message_type = (message.get("message_type") or message.get("msg_type") or "").strip()
        parent_id = (message.get("parent_id") or "").strip()
        root_id = (message.get("root_id") or "").strip()
        thread_id = (message.get("thread_id") or "").strip()

        # ── 发送者 ──
        sender_id_obj = sender.get("sender_id", {}) or {}
        sender_open_id = sender_id_obj.get("open_id", "") or sender_id_obj.get("user_id", "")
        sender_type = sender.get("sender_type", "")
        # 优先从 sender 获取名字，否则查 API
        sender_name = sender.get("name", "") or ""

        # ── 文本内容 ──
        text, image_keys = self._extract_content(
            message, message_type, message_id,
        )

        # ── 发送者名称（API 查） ──
        if not sender_name:
            try:
                api_name = get_user_name(sender_open_id)
                if api_name:
                    sender_name = api_name
            except Exception:
                pass
        # 如果 API 查不到，从 mentions 里捞名字（避免显示 raw open_id）
        if not sender_name:
            for m in message.get("mentions") or []:
                mid = m.get("id") or {}
                if isinstance(mid, dict):
                    m_oid = str(mid.get("open_id", "") or mid.get("user_id", ""))
                    if m_oid == sender_open_id:
                        sender_name = str(m.get("name", "") or "").strip()
                        break
        if not sender_name:
            sender_name = "用户"

        # ── 回复原文 ──
        reply_to_text = ""
        if parent_id:
            try:
                reply_msg = get_message_by_id(parent_id)
                if reply_msg:
                    reply_to_text = parse_message_content(
                        reply_msg.get("content", ""),
                        reply_msg.get("msg_type", "text"),
                    )
            except Exception:
                pass

        # ── 构建 MessageSource ──
        source = MessageSource(
            platform="feishu",
            sender_id=sender_open_id,
            sender_name=sender_name or sender_open_id,
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type=chat_type,
            thread_id=thread_id,
        )

        # ── 构建 Mentions ──
        mention_refs = self._parse_mentions(
            message.get("mentions", []), sender_open_id,
        )

        # ── 构建 MessageContext ──
        ctx = MessageContext(
            text=text,
            message_type=message_type,
            message_id=message_id,
            parent_id=parent_id,
            root_id=root_id,
            reply_to_text=reply_to_text,
            mentions=mention_refs,
        )

        return source, ctx

    def is_group_mention_bot(self, event_data: dict) -> bool:
        """群聊中检查是否 @了机器人或 @all。"""
        event = event_data.get("event", {})
        message = event.get("message", {})
        chat_type = (message.get("chat_type") or "").strip()
        if chat_type != "group":
            return True  # 私聊不需要 @
        mentions = message.get("mentions") or []
        if len(mentions) > 0:
            return True
        # @all 时 mentions 列表可能为空，检查 content 中的 @ 标记
        content = message.get("content") or ""
        if "@_all" in content or "@所有人" in content:
            return True
        return False

    def is_image_message(self, event_data: dict) -> bool:
        event = event_data.get("event", {})
        mtype = (event.get("message", {}).get("message_type") or "").strip()
        return mtype == "image"

    def is_file_message(self, event_data: dict) -> bool:
        event = event_data.get("event", {})
        mtype = (event.get("message", {}).get("message_type") or "").strip()
        return mtype == "file"

    # ── 内部方法 ──

    def _extract_content(
        self, message: dict, message_type: str, message_id: str,
    ) -> Tuple[str, List[str]]:
        """提取消息文本内容和图片 key。"""
        if message_type == "image":
            keys = extract_image_keys_from_event_message(message)
            return "", keys

        if message_type == "file":
            text = self._handle_file(message, message_id)
            return text, []

        text = extract_text_from_event_message(message)
        return text or "", []

    def _handle_file(self, message: dict, message_id: str) -> str:
        """下载文件并生成文本描述。"""
        file_info = extract_file_info_from_event_message(message)
        if not file_info:
            return ""
        file_key = file_info["file_key"]
        file_name = file_info["file_name"]

        dl_result = download_message_resource(message_id, file_key, resource_type="file")
        if not dl_result:
            return f"[文件: {file_name} 下载失败]"

        file_bytes, content_type = dl_result
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

        if ext in _TEXT_EXTS:
            try:
                file_text = file_bytes.decode("utf-8")
                return f"[文件: {file_name}]\n\n```\n{file_text}\n```"
            except UnicodeDecodeError:
                return f"[文件: {file_name} (无法解码为文本，大小 {len(file_bytes):,} 字节)]"
        else:
            return f"[文件: {file_name} (大小 {len(file_bytes):,} 字节)]"

    def _parse_mentions(self, mentions: list, sender_open_id: str) -> List[MentionRef]:
        """解析 @提及列表。"""
        result = []
        for m in mentions or []:
            key = str(m.get("key", "") or "")
            name = str(m.get("name", "") or "").strip()
            mid = m.get("id") or {}
            oid = ""
            if isinstance(mid, dict):
                oid = str(mid.get("open_id", "") or "") or str(mid.get("user_id", "") or "")
            is_self = bool(
                oid
                and self._context_builder._bot_id
                and oid == self._context_builder._bot_id
            )
            result.append(MentionRef(
                key=key or "",
                name=name or oid or "用户",
                user_id=oid,
                is_bot=is_self,
                is_all=(key == "@_all"),
            ))
        return result

    def _get_image_keys(self, event_data: dict) -> List[str]:
        """从图片消息事件中提取 image_key。"""
        from .api import extract_image_keys_from_event_message
        message = event_data.get("event", {}).get("message", {})
        return extract_image_keys_from_event_message(message)

    @staticmethod
    def extract_img_ids_from_text(s: str) -> List[str]:
        """从文本中提取 img-xxx 引用。"""
        ids = re.findall(r"\bimg-[0-9a-fA-F-]{6,}\b", s or "")
        seen = set()
        out = []
        for i in ids:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out[:20]

    @staticmethod
    def convert_sdk_event_to_dict(data) -> Optional[Dict[str, Any]]:
        """把 lark_oapi SDK 事件对象转成普通 dict。"""
        if not data.event or not data.event.message:
            return None
        msg = data.event.message
        mentions = []
        if msg.mentions:
            for m in msg.mentions:
                mention_dict: Dict[str, Any] = {
                    "key": m.key or "",
                    "name": m.name or "",
                    "tenant_key": m.tenant_key or "",
                }
                if m.id:
                    mid: Dict[str, str] = {}
                    for attr in ("user_id", "open_id", "union_id"):
                        val = getattr(m.id, attr, None)
                        if val:
                            mid[attr] = val
                    if mid:
                        mention_dict["id"] = mid
                mentions.append(mention_dict)
        sender_dict: Dict[str, Any] = {}
        if data.event.sender:
            sd = data.event.sender
            sid_obj: Dict[str, str] = {}
            if sd.sender_id:
                for attr in ("open_id", "user_id", "union_id"):
                    val = getattr(sd.sender_id, attr, None)
                    if val:
                        sid_obj[attr] = val
            sender_dict = {
                "sender_id": sid_obj,
                "sender_type": sd.sender_type or "",
                "tenant_key": sd.tenant_key or "",
            }
        return {
            "header": {"event_type": data.header.event_type if data.header else "im.message.receive_v1"},
            "event": {
                "sender": sender_dict,
                "message": {
                    "chat_id": msg.chat_id or "",
                    "chat_type": msg.chat_type or "",
                    "message_id": msg.message_id or "",
                    "message_type": msg.message_type or "",
                    "content": msg.content or "",
                    "mentions": mentions,
                    "parent_id": msg.parent_id or "",
                    "root_id": msg.root_id or "",
                    "thread_id": getattr(msg, "thread_id", None) or "",
                }
            },
        }
