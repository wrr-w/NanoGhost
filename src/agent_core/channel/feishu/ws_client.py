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
from agent_core.channel.message_context import ContextBuilder, MessageSource, MessageContext
from agent_core.channel.interfaces import ChannelIO
from agent_core.presenter import run_agent_turn

from .tools import register_feishu_tools
from .sdk import FeishuSDK
from .turn import FeishuTurnParser
from .io import FeishuIO
from .channel import FeishuChannel
from . import api

# P0：通道注册表 + 端点目录
from agent_core.channel.route import RouteEnvelope
from agent_core.channel.registry import register_channel
from agent_core.channel.directory import register_endpoint

# P1：收件箱 + 常驻消费者
from agent_core.runtime.inbox import get_hub
from agent_core.runtime.consumer import ResidentConsumer

# P2：出站路由 + 出站镜像
from agent_core.router import get_router
from agent_core.channel.endpoint import parse_addr

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
        lark_cli_profile = os.environ.get("LARK_CLI_PROFILE", "")
        self._context_builder = ContextBuilder(
            bot_name=bot_name, bot_id=bot_id, lark_cli_profile=lark_cli_profile,
        )

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

        # 通道注册（P0）：把飞书套成统一 Channel 并登记进注册表
        self.channel = FeishuChannel(io=self.io)
        try:
            register_channel(self.channel)
        except Exception:
            logger.exception("[Feishu WS] register channel failed")

        # 常驻消费者（P1）：所有入站消息先入「每端点队列」，再由此处按忙闲消费
        self.consumer = ResidentConsumer(get_hub(), self._handle_inbox_batch)

        # 出站镜像（P2）：主动发到「别的端点」时，把这条也写进【目标会话】
        try:
            get_router().set_mirror(self._mirror_outbound)
        except Exception:
            logger.exception("[Feishu WS] set router mirror failed")

        # 注册飞书特有工具（lookup_user 等，替代全量群成员 dump）
        register_feishu_tools(self.agent, self.sessions.mention_name_map)

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
                if not self._context_builder._lark_cli_profile:
                    self._context_builder._lark_cli_profile = self._context_builder._bot_name
                logger.info(f"[Feishu WS] auto-resolved bot_id={bot_id} name={bot_name}")
            else:
                logger.warning(f"[Feishu WS] get_bot_info returned no data, bot_id remains empty. "
                               f"Check Lark app scope or network.")
        except Exception as e:
            logger.exception(f"[Feishu WS] failed to auto-resolve bot_id: {e}")

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

        # 启动常驻消费者（P1）：与 WS 并列；所有入站消息都经它消费
        try:
            self.consumer.start()
        except Exception:
            logger.exception("[Feishu WS] consumer start failed")

        # 启动子 Agent 池（P3）：后台子任务并行执行 + 完成回报
        try:
            from agent_core.runtime.subagent_pool import get_pool
            get_pool()
        except Exception:
            logger.exception("[Feishu WS] subagent pool start failed")

        # 启动 Watcher（P4）：盯变化的「事件源」（无 watch 时为空转）
        try:
            from agent_core.runtime.watcher import get_watcher
            get_watcher().start()
        except Exception:
            logger.exception("[Feishu WS] watcher start failed")

        self.sdk.start()
        try:
            while self._running:
                if not self.sdk.is_alive():
                    logger.warning("[Feishu WS] SDK thread died, restarting in 5s")
                    await asyncio.sleep(5)
                    if self._running:
                        self.sdk.start()
                await asyncio.sleep(1)
        finally:
            try:
                self.consumer.stop()
            except Exception:
                logger.exception("[Feishu WS] consumer stop failed")

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

            expired = [k for k, t in self._seen_events.items() if now - t > 120]
            for k in expired:
                del self._seen_events[k]

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

            # 入队（P1）：所有入站消息先进「每端点队列」，由常驻消费者按忙闲消费
            try:
                get_router().submit(
                    RouteEnvelope(
                        direction="inbound",
                        kind="channel_message",
                        target_addr=f"feishu:{chat_id}",
                        source_addr="feishu",
                        payload=event_data,
                        summary=f"{chat_type} {sender_open_id}",
                    )
                )
            except Exception:
                logger.exception("[Feishu WS] consumer submit failed, fallback to thread")
                threading.Thread(
                    target=lambda: asyncio.run(self._process(event_data)),
                    daemon=True,
                ).start()
        except Exception:
            logger.exception("[Feishu WS] SDK callback error")

    async def _handle_inbox_batch(self, target: str, events) -> None:
        """常驻消费者回调（P1/P3）：处理某端点的一批事件（端点内串行）。

        事件类别：
          · channel_message → 走既有的 _process（一条消息一轮）
          · timer           → 定时任务（交给 scheduler.run_task_once）
          · subagent_done   → 后台子任务完成（父 agent 闲 → 起一轮告知）
        """
        for ev in events:
            try:
                if ev.kind == "channel_message":
                    await self._process(ev.payload)
                elif ev.kind == "timer":
                    from agent_core.scheduler import run_task_once
                    task = (ev.payload or {}).get("task")
                    if task is not None:
                        await run_task_once(self, task)
                elif ev.kind == "subagent_done":
                    await self._on_subagent_done(target, ev.payload or {})
                else:
                    logger.info("[Feishu WS] ignore event kind=%s (target=%s)", ev.kind, target)
            except Exception:
                logger.exception("[Feishu WS] handle event failed (kind=%s target=%s)", ev.kind, target)

    async def _on_subagent_done(self, target: str, payload: Dict[str, Any]) -> None:
        """后台子任务完成（P3）：父 agent 空闲时，起一轮把完成结果告知它。"""
        desc = payload.get("description") or ""
        status = payload.get("status") or ""
        if status == "done":
            body = f"[后台子任务完成] 「{desc}」已完成：\n{(payload.get('result') or '')[:2000]}"
        else:
            body = f"[后台子任务失败] 「{desc}」执行失败：{payload.get('error')}"
        _, chat_id = parse_addr(target)
        logger.info("[Feishu WS] subagent_done → 起一轮告知 chat_id=%s run_id=%s", chat_id, payload.get("run_id"))
        source = MessageSource(
            platform="feishu",
            chat_id=chat_id,
            chat_name="后台子任务",
            chat_type="p2p",
            sender_id="__subagent__",
            sender_name="后台子任务",
            is_bot=True,
        )
        ctx = MessageContext(text=body, message_type="text", message_id="")
        user_text = self._context_builder.build_user_message(source, ctx)
        await run_agent_turn(
            agent=self.agent,
            identity=self.instance,
            sessions=self.sessions,
            io=self.io,
            context_builder=self._context_builder,
            source=source,
            ctx=ctx,
            user_text=user_text,
            images_base64=None,
            base_url=self._base_url,
            api_spec=self._api_spec,
            channel_ctx={"chat_id": chat_id, "platform": "feishu",
                         "subagent_done": True, "run_id": payload.get("run_id")},
        )

    def _mirror_outbound(self, addr: str, text: str) -> None:
        """出站镜像（P2）：把主动发到「别的端点」的消息写进**目标会话**历史。

        由 router 在投递成功后回调（仅当 target != 来源会话）。
        """
        try:
            _, chat_id = parse_addr(addr)
            if not chat_id:
                return
            session_id, _ = self.sessions.get_or_create(chat_id)
            self.agent.db.add_agent_message(session_id, "assistant", text, type="text")
            logger.info("[Feishu WS] mirror → %s (session=%s)", addr, session_id)
        except Exception:
            logger.exception("[Feishu WS] mirror outbound failed addr=%s", addr)

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
        is_mention = self.turn.is_group_mention_bot(event_data, self.instance.bot_id or "", self.instance.bot_name or "")
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

        # 3.7 端点动态注册（P0）：收到消息即登记「这个会话端点」
        try:
            kind = "group" if source.is_group else "user"
            register_endpoint(
                f"feishu:{source.chat_id}",
                channel="feishu",
                kind=kind,
                capabilities=self.channel.capabilities(),
                meta={
                    "chat_id": source.chat_id,
                    "chat_name": source.chat_name,
                    "chat_type": source.chat_type,
                    "sender_id": source.sender_id,
                    "sender_name": source.sender_name,
                },
            )
        except Exception:
            logger.exception("[Feishu WS] register endpoint failed")

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


