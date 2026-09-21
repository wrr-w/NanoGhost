# -*- coding: utf-8 -*-
"""FeishuTransport —— 飞书「接收管道」。

只做「接收」：SDK 长连接 + 回调去重 + 每端点收件箱消费。
**不含渠道逻辑** —— 收到消息后交给 FeishuChannel（渠道门面）处理。

组合关系：FeishuChannel 持有 FeishuTransport（乙b）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Dict

from agent_core.channel.route import RouteEnvelope
from agent_core.router import get_router
from agent_core.runtime.inbox import get_hub
from agent_core.runtime.consumer import ResidentConsumer

from .sdk import FeishuSDK
from .turn import FeishuTurnParser

logger = logging.getLogger("agent_core")


class FeishuTransport:
    """飞书接收管道（WS 长连接 + 每端点收件箱消费）。"""

    def __init__(self, channel: Any) -> None:
        self.channel = channel
        self.log = getattr(channel, "log", logger)

        # 复用渠道上的共享依赖（保持原 ws_client 的写法）
        self.agent = channel.agent
        self.instance = channel.instance
        self.sessions = channel.sessions
        self.io = channel.io
        self._context_builder = channel.context_builder
        self._base_url = channel._base_url
        self._api_spec = channel._api_spec

        self._running = False
        # 事件去重
        self._seen_events: Dict[str, float] = {}

        # 飞书特有：SDK 长连接
        self.sdk = FeishuSDK()
        self.sdk.set_message_handler(self._on_sdk_message)

        # 常驻消费者（P1）：所有入站消息先入「每端点队列」，再由此处按忙闲消费
        self.consumer = ResidentConsumer(get_hub(), self._handle_inbox_batch)

    # ════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════

    def _ensure_bot_id(self) -> None:
        """若 bot_id 未配置，自动从飞书 API 获取当前 bot 的 open_id。"""
        if self.instance.bot_id:
            return
        try:
            from . import api
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
                logger.info(f"[Feishu] auto-resolved bot_id={bot_id} name={bot_name}")
            else:
                logger.warning(f"[Feishu] get_bot_info returned no data, bot_id remains empty. "
                               f"Check Lark app scope or network.")
        except Exception as e:
            logger.exception(f"[Feishu] failed to auto-resolve bot_id: {e}")

    async def start(self) -> None:
        await self.run_forever()

    async def stop(self) -> None:
        self._running = False
        logger.info("[Feishu] stopped")

    async def run_forever(self) -> None:
        self._running = True

        if not os.getenv("FEISHU_APP_ID") or not os.getenv("FEISHU_APP_SECRET"):
            logger.warning("[Feishu] FEISHU_APP_ID/FEISHU_APP_SECRET not configured")
            return

        # 启动常驻消费者（P1）：与 WS 并列；所有入站消息都经它消费
        try:
            self.consumer.start()
        except Exception:
            logger.exception("[Feishu] consumer start failed")

        # 启动子 Agent 池（P3）：后台子任务并行执行 + 完成回报
        try:
            from agent_core.runtime.subagent_pool import get_pool
            get_pool()
        except Exception:
            logger.exception("[Feishu] subagent pool start failed")

        # 启动 Watcher（P4）：盯变化的「事件源」（无 watch 时为空转）
        try:
            from agent_core.runtime.watcher import get_watcher
            get_watcher().start()
        except Exception:
            logger.exception("[Feishu] watcher start failed")

        self.sdk.start()
        try:
            while self._running:
                if not self.sdk.is_alive():
                    logger.warning("[Feishu] SDK thread died, restarting in 5s")
                    await asyncio.sleep(5)
                    if self._running:
                        self.sdk.start()
                await asyncio.sleep(1)
        finally:
            try:
                self.consumer.stop()
            except Exception:
                logger.exception("[Feishu] consumer stop failed")

    # ════════════════════════════════════════════
    # SDK 回调
    # ════════════════════════════════════════════

    def _on_sdk_message(self, data) -> None:
        try:
            event_data = FeishuTurnParser.convert_sdk_event_to_dict(data)
            if not event_data:
                logger.info("[Feishu] convert_sdk_event_to_dict returned None")
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
                logger.info(f"[Feishu] DUPLICATE msg_id={message_id} chat_id={chat_id}")
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
                f"[Feishu] RECEIVED msg_id={message_id} chat_id={chat_id} "
                f"chat_type={chat_type} sender={sender_open_id} "
                f"msg_type={msg.get('message_type')} "
                f"mentions={mentions_summary} "
                f"mentions_count={len(mentions)} "
                f"bot_id={self.instance.bot_id}"
            )
            content_preview = str(msg.get("content", ""))[:200].replace("\n", " ")
            logger.info(f"[Feishu] CONTENT preview={content_preview}")

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
                logger.exception("[Feishu] consumer submit failed, fallback to thread")
                threading.Thread(
                    target=lambda: asyncio.run(self.channel.handle_message(event_data)),
                    daemon=True,
                ).start()
        except Exception:
            logger.exception("[Feishu] SDK callback error")

    async def _handle_inbox_batch(self, target: str, events) -> None:
        """常驻消费者回调（P1/P3）：处理某端点的一批事件（端点内串行）。"""
        for ev in events:
            try:
                if ev.kind == "channel_message":
                    await self.channel.handle_message(ev.payload)
                elif ev.kind == "timer":
                    from agent_core.scheduler import run_task_once
                    task = (ev.payload or {}).get("task")
                    if task is not None:
                        await run_task_once(self.channel, task)
                elif ev.kind == "subagent_done":
                    await self.channel.on_subagent_done(target, ev.payload or {})
                else:
                    logger.info("[Feishu] ignore event kind=%s (target=%s)", ev.kind, target)
            except Exception:
                logger.exception("[Feishu] handle event failed (kind=%s target=%s)", ev.kind, target)


__all__ = ["FeishuTransport"]
