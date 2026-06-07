# -*- coding: utf-8 -*-
"""
飞书渠道编排器。

职责：组合通用层 + 飞书特有层，不做业务逻辑。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from agent_core.engine.agent import Agent
from agent_core.channel.instance import BotInstance
from agent_core.channel.session import SessionStore
from agent_core.channel.message_context import ContextBuilder
from agent_core.channel.interfaces import ChannelIO
from agent_core.presenter import run_agent_turn

from .sdk import FeishuSDK
from .turn import FeishuTurnParser
from .io import FeishuIO
from . import api

logger = logging.getLogger("agent_core")


class FeishuWSClient:
    """飞书渠道编排器。"""

    def __init__(
        self,
        agent: Agent,
        sys_prompt: str = "",
        api_spec: Optional[Dict] = None,
        base_url: str = "",
    ) -> None:
        self.agent = agent
        self._base_url = base_url or os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        self._api_spec = api_spec or {}
        self._running = False

        bot_name = os.environ.get("FEISHU_BOT_NAME", "")
        # bot_id 优先从环境变量读取，未配置时自动从飞书 API 获取
        bot_id = os.environ.get("FEISHU_BOT_OPEN_ID", "")
        self._context_builder = ContextBuilder(bot_name=bot_name, bot_id=bot_id)

        # 通用层
        self.instance = BotInstance(sys_prompt, bot_name=bot_name, bot_id=bot_id)
        self.instance.load_feedback_level()
        self.instance.load_history_limits()
        self.sessions = SessionStore(agent, self._context_builder, get_bot_id=lambda: self.instance.bot_id)

        # 飞书特有层
        self.sdk = FeishuSDK()
        self.turn = FeishuTurnParser(self._context_builder)
        self.io = FeishuIO()
        self.io.set_name_map(self.sessions.mention_name_map)

        # 事件去重
        self._seen_events: Dict[str, float] = {}

        self.sdk.set_message_handler(self._on_sdk_message)

    def _ensure_bot_id(self) -> None:
        """若 bot_id 未配置，自动从飞书 API 获取当前 bot 的 open_id。"""
        if self.instance.bot_id:
            return
        try:
            info = api.get_bot_info()
            if info and info.get("open_id"):
                bot_id = info["open_id"]
                bot_name = info.get("name", self.instance.bot_name)
                self.instance.bot_id = bot_id
                self.instance.bot_name = bot_name or self.instance.bot_name
                self._context_builder._bot_id = bot_id
                self._context_builder._bot_name = bot_name or self._context_builder._bot_name
                # SessionStore 通过 get_bot_id=lambda: self.instance.bot_id 自动对齐
                logger.info(f"[Feishu WS] auto-resolved bot_id={bot_id} name={bot_name}")
        except Exception:
            logger.exception("[Feishu WS] failed to auto-resolve bot_id")

    async def start(self) -> None:
        await self.run_forever()

    async def stop(self) -> None:
        self._running = False
        logger.info("[Feishu WS] stopped")

    async def run_forever(self) -> None:
        self._running = True

        if not os.getenv("FEISHU_APP_ID") or not os.getenv("FEISHU_APP_SECRET"):
            logger.warning("[Feishu WS] FEISHU_APP_ID/FEISHU_APP_SECRET not configured")
            return

        self.sdk.start()
        while self._running:
            if not self.sdk.is_alive():
                logger.warning("[Feishu WS] SDK thread died, restarting in 5s")
                await asyncio.sleep(5)
                if self._running:
                    self.sdk.start()
            await asyncio.sleep(1)

    # ════════════════════════════════════════════
    # SDK 回调
    # ════════════════════════════════════════════

    def _on_sdk_message(self, data) -> None:
        try:
            event_data = FeishuTurnParser.convert_sdk_event_to_dict(data)
            if not event_data:
                logger.info("[Feishu WS] convert_sdk_event_to_dict returned None")
                return

            # 去重
            msg = event_data["event"]["message"]
            chat_id = msg.get("chat_id", "")
            message_id = msg.get("message_id", "")
            now = time.time()
            if message_id in self._seen_events and (now - self._seen_events[message_id]) < 60:
                logger.info(f"[Feishu WS] DUPLICATE msg_id={message_id} chat_id={chat_id}")
                return
            self._seen_events[message_id] = now

            sender = event_data.get("event", {}).get("sender", {})
            sender_id_obj = sender.get("sender_id", {}) or {}
            sender_open_id = sender_id_obj.get("open_id", "") or sender_id_obj.get("user_id", "")
            chat_type = msg.get("chat_type", "")
            mentions = msg.get("mentions", [])
            mentions_summary = [
                f"{m.get('name','?')}({m.get('id',{}).get('open_id','')})"
                for m in mentions
            ]

            logger.info(
                f"[Feishu WS] RECEIVED msg_id={message_id} chat_id={chat_id} "
                f"chat_type={chat_type} sender={sender_open_id} "
                f"msg_type={msg.get('message_type')} "
                f"mentions={mentions_summary} "
                f"mentions_count={len(mentions)} "
                f"bot_id={self.instance.bot_id}"
            )
            # 打印原始 content 前 200 字（调试用）
            content_preview = str(msg.get("content", ""))[:200].replace("\n", " ")
            logger.info(f"[Feishu WS] CONTENT preview={content_preview}")

            # 自动解析 bot_id（首次收到消息时）
            self._ensure_bot_id()

            threading.Thread(
                target=lambda: asyncio.run(self._process(event_data)),
                daemon=True,
            ).start()
        except Exception:
            logger.exception("[Feishu WS] SDK callback error")

    # ════════════════════════════════════════════
    # 编排流程
    # ════════════════════════════════════════════

    async def _process(self, event_data: dict) -> None:
        """编排一条消息的完整生命周期。"""
        if event_data.get("header", {}).get("event_type", "") != "im.message.receive_v1":
            logger.info("[Feishu WS] SKIP event_type != im.message.receive_v1")
            return

        # 1. 图片→缓存
        if self.turn.is_image_message(event_data):
            message = event_data["event"]["message"]
            keys = self.turn._get_image_keys(event_data)
            if keys and message.get("message_id"):
                self.sessions.cache_image_keys(
                    message["chat_id"], message["message_id"], keys
                )
            logger.info(f"[Feishu WS] IMAGE cached keys={keys}")
            return

        # 2. 群聊未@bot→跳过
        is_mention = self.turn.is_group_mention_bot(event_data)
        logger.info(f"[Feishu WS] is_group_mention_bot={is_mention}")
        if not is_mention:
            logger.info("[Feishu WS] SKIP not mention bot")
            return

        # 3. 解析
        source, ctx = self.turn.parse_event(event_data)
        logger.info(
            f"[Feishu WS] PARSED sender_id={source.sender_id} "
            f"sender_name={source.sender_name} chat_id={source.chat_id} "
            f"chat_type={source.chat_type} text_preview={ctx.text[:80] if ctx.text else '(empty)'} "
            f"mentions=[{', '.join(f'{m.name}({m.user_id})' for m in ctx.mentions)}] "
            f"bot_mentions=[{', '.join(f'{m.name}({m.user_id})' for m in ctx.bot_mentions)}] "
            f"is_bot_sender={source.is_bot}"
        )

        # 3.5 缓存 mention name -> user_id 映射（用于 bot 回复时 @人）
        if source.sender_name and source.sender_id:
            self.sessions.record_mention(source.chat_id, source.sender_name, source.sender_id)
        for m in ctx.mentions:
            if m.name and m.user_id:
                self.sessions.record_mention(source.chat_id, m.name, m.user_id)

        # 3.6 过滤：bot 自己不处理自己发的消息
        if self.instance.bot_id and source.sender_id == self.instance.bot_id:
            logger.info("[Feishu WS] SKIP message sent by self")
            return

        if not ctx.text and not ctx.mentions:
            logger.info("[Feishu WS] SKIP empty text and no mentions")
            return

        # 4. 斜杠命令
        text = ctx.text
        chat_id = source.chat_id
        mid = ctx.message_id
        if text.startswith("/new") or text.startswith("/reset"):
            self.sessions.reset(chat_id)
            api.send_text_message_to_chat(chat_id, "Done")
            return
        if text.startswith("/stop"):
            api.send_text_message_to_chat(chat_id, "Stopped")
            return
        if text.startswith("/img"):
            self._handle_img(chat_id, text)
            return

        # 5. 消费图片缓存
        cached = self.sessions.consume_image_cache(chat_id)
        logger.info(f"[Feishu WS] image_cache consumed={len(cached)}")

        # 6. 下载图片→base64
        images_base64 = []
        if cached:
            for r in cached:
                mid = (r or {}).get("message_id") or ""
                fk = (r or {}).get("file_key") or ""
                dl = self.io.download_image(mid, fk)
                if not dl:
                    continue
                img_bytes, content_type = dl
                ext = "png"
                if content_type and "image/" in content_type:
                    ext = content_type.split(";")[0].split("/")[-1].strip() or "png"
                if ext == "jpg":
                    ext = "jpeg"
                mime = f"image/{ext}" if ext in ("png", "jpeg", "gif", "webp") else "image/png"
                import base64
                b64 = f"data:{mime};base64,{base64.b64encode(img_bytes).decode('ascii')}"
                if b64:
                    images_base64.append(b64)

        # 7. 格式化用户文本
        user_text = self._context_builder.build_user_message(source, ctx)
        logger.info(f"[Feishu WS] USER_TEXT for LLM={user_text[:200]}")

        # 8. Agent 执行（通用循环）
        session_id, is_new = self.sessions.get_or_create(chat_id)
        logger.info(f"[Feishu WS] SESSION session_id={session_id} is_new={is_new}")

        await run_agent_turn(
            agent=self.agent,
            identity=self.instance,
            sessions=self.sessions,
            io=self.io,
            context_builder=self._context_builder,
            source=source,
            ctx=ctx,
            user_text=user_text,
            images_base64=images_base64 or None,
            base_url=self._base_url,
            api_spec=self._api_spec,
        )

    def _handle_img(self, chat_id: str, text: str):
        """处理 /img 命令（飞书特有）。"""
        parts = [p for p in text.split() if p.strip()]
        ids = [
            p.replace("/agent-images/", "", 1) if p.startswith("/agent-images/") else p
            for p in parts[1:]
        ]
        ids = [x for x in ids if x]
        if not ids:
            api.send_text_message_to_chat(chat_id, "Usage: /img img-xxx img-yyy")
            return
        rows = self.agent.db.get_agent_images_batch(ids) or []
        b64 = [r["base64"] for r in rows if isinstance(r, dict) and r.get("base64")]
        if not b64:
            api.send_text_message_to_chat(chat_id, "No images found")
            return
        r = api.send_images_base64_to_chat(chat_id, b64)
        if not r.get("ok"):
            api.send_text_message_to_chat(chat_id, f"Failed: sent={r.get('sent')} failed={r.get('failed')}")
