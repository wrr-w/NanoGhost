# -*- coding: utf-8 -*-
"""
通用 Session 层：会话生命周期、上下文缓存。

所有渠道共用，不依赖具体平台 API。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from agent_core.channel.message_context import ContextBuilder, MessageSource

logger = logging.getLogger("agent_core")


class SessionStore:
    """通用 Session 管理器。

    所有渠道共用同一套 session 创建/查找/缓存逻辑。
    """

    def __init__(self, agent, context_builder: ContextBuilder, get_bot_id=None):
        self.agent = agent
        self._context_builder = context_builder
        self._get_bot_id = get_bot_id or (lambda: "")
        # session 上下文块缓存: chat_id -> str
        self._context_cache: Dict[str, str] = {}
        # 图片缓存: chat_id -> {resources: [...], expire_at: float}
        self._image_cache: Dict[str, Dict] = {}
        self._image_cache_lock = threading.Lock()
        # @提及 name -> open_id 映射（用于 bot 回复时反查）
        self.mention_name_map: Dict[str, str] = {}
        # 已加载持久化 mentions 的 chat_id 集合（避免重复加载）
        self._loaded_mention_chats: set = set()
        self._mention_lock = threading.Lock()

    def _session_key(self, chat_id: str) -> str:
        """生成 session 查询用的 key，支持多 bot 隔离。"""
        bot_id = self._get_bot_id()
        if bot_id:
            return f"{bot_id}:{chat_id}"
        return chat_id

    # ── Session 生命周期 ──

    def get_or_create(self, chat_id: str) -> Tuple[str, bool]:
        """获取或创建 session，返回 (session_id, is_new)。"""
        key = self._session_key(chat_id)
        session_id = None
        chat_row = self.agent.db.get_chat_session(key)
        if chat_row:
            session_id = chat_row.get("session_id")
            existing = self.agent.db.get_agent_session(session_id)
            if not existing:
                session_id = None
                self.agent.db.delete_chat_session(key)
        if session_id:
            # 首次看到这个 chat_id 时从持久化加载 mentions
            self._ensure_mentions_loaded(chat_id)
            return session_id, False
        session_id = self.agent.db.create_agent_session(f"session:{key[:8]}")
        self.agent.db.set_chat_session(key, session_id)
        self._ensure_mentions_loaded(chat_id)
        return session_id, True

    def _ensure_mentions_loaded(self, chat_id: str) -> None:
        """若尚未加载该 chat_id 的持久化 mentions，则从数据库加载。"""
        if chat_id in self._loaded_mention_chats:
            return
        try:
            rows = self.agent.db.load_chat_mentions(chat_id)
            with self._mention_lock:
                for row in rows:
                    name = row.get("name")
                    uid = row.get("user_id")
                    if name and uid:
                        self.mention_name_map[name] = uid
            self._loaded_mention_chats.add(chat_id)
        except Exception:
            logger.exception(f"[Session] load_chat_mentions failed chat_id={chat_id}")

    def record_mention(self, chat_id: str, name: str, user_id: str) -> None:
        """记录一个 name->user_id 映射到内存，并异步写入数据库。"""
        if not name or not user_id:
            return
        with self._mention_lock:
            self.mention_name_map[name] = user_id
        try:
            self.agent.db.save_chat_mention(chat_id, name, user_id)
        except Exception:
            logger.exception(f"[Session] save_chat_mention failed chat_id={chat_id} name={name}")

    def reset(self, chat_id: str):
        """清空 session（保留 mention 映射持久化）。"""
        try:
            key = self._session_key(chat_id)
            chat_row = self.agent.db.get_chat_session(key)
            if chat_row:
                session_id = chat_row.get("session_id")
                if session_id:
                    self.agent.db.delete_agent_session(session_id)
                self.agent.db.delete_chat_session(key)
            self._context_cache.pop(chat_id, None)
            self._image_cache.pop(chat_id, None)
            # 注意：mention_name_map 和 _loaded_mention_chats 不清理，
            # 因为 mentions 是 chat 级别的成员信息，和对话历史无关
        except Exception:
            logger.exception(f"Session reset failed chat_id={chat_id}")

    # ── Session 上下文块 ──

    def get_context_block(self, source: MessageSource) -> str:
        """获取或构建 session 上下文块，缓存复用。

        渠道适配器可在调用前修改 source.chat_name 以显示群名。
        """
        key = source.chat_id
        if key in self._context_cache:
            return self._context_cache[key]

        block = self._context_builder.build_session_context(
            source, shared_session=source.is_group
        )
        self._context_cache[key] = block
        return block

    def invalidate_context_block(self, chat_id: str):
        """强制清除上下文缓存（群信息变更时调用）。"""
        self._context_cache.pop(chat_id, None)

    # ── 图片缓存 ──

    def cache_image_keys(self, chat_id: str, message_id: str, keys: List[str]):
        with self._image_cache_lock:
            now = time.time()
            entry = self._image_cache.get(chat_id) or {}
            expire_at = float(entry.get("expire_at") or 0)
            if expire_at < now:
                entry = {}
            resources = entry.get("resources") or []
            if not isinstance(resources, list):
                resources = []
            for k in keys:
                resources.append({"message_id": message_id, "file_key": k})
            entry["resources"] = resources
            entry["expire_at"] = now + 300
            self._image_cache[chat_id] = entry

    def consume_image_cache(self, chat_id: str) -> List[Dict[str, str]]:
        with self._image_cache_lock:
            now = time.time()
            entry = self._image_cache.get(chat_id) or {}
            expire_at = float(entry.get("expire_at") or 0)
            if expire_at < now:
                self._image_cache.pop(chat_id, None)
                return []
            resources = entry.get("resources") or []
            self._image_cache.pop(chat_id, None)
            return resources
