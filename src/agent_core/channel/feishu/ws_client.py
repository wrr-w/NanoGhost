"""
飞书 WebSocket 事件监听客户端（基于 lark-oapi SDK）。

启动后在后台线程建立长连接,收到消息后调用 Agent 处理。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from agent_core.interfaces import DatabasePort, LLMPort, HttpPort, ImagePort
from agent_core.engine.config import AgentConfig
from agent_core.agent import Agent
from agent_core.channel.base import ChannelPort

from .api import (
    download_message_resource,
    extract_image_keys_from_event_message,
    extract_text_from_event_message,
    send_images_base64_to_chat,
    send_markdown_message_to_chat,
    send_text_message_to_chat,
)

logger = logging.getLogger("agent_core")


class FeishuWSClient(ChannelPort):
    """飞书 WebSocket 长连接客户端（基于 lark-oapi SDK）。

    接收飞书消息事件,转发给 Agent 处理并回发回复。
    """

    def __init__(
        self,
        agent: Agent,
        sys_prompt: str = "",
        api_spec: Optional[Dict] = None,
        base_url: str = "",
    ) -> None:
        self.agent = agent
        self._sys_prompt = sys_prompt
        self._api_spec = api_spec or {}
        self._base_url = base_url or os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 图片缓存
        self._image_cache: Dict[str, Dict] = {}
        self._image_cache_lock = threading.Lock()

    async def start(self) -> None:
        """启动通道监听（ChannelPort 接口）。"""
        await self.run_forever()

    async def stop(self) -> None:
        """停止通道监听（ChannelPort 接口）。"""
        self._running = False
        logger.info("[Feishu WS] 已停止")

    async def run_forever(self) -> None:
        """持续运行,建立 WebSocket 长连接。"""
        self._running = True

        if not os.getenv("FEISHU_APP_ID") or not os.getenv("FEISHU_APP_SECRET"):
            logger.warning("[Feishu WS] FEISHU_APP_ID/FEISHU_APP_SECRET 未配置")
            return

        self._start_sdk_thread()

        while self._running:
            if not self._thread or not self._thread.is_alive():
                logger.warning("[Feishu WS] 连接线程已退出,5s 后重建")
                await asyncio.sleep(5)
                if self._running:
                    self._start_sdk_thread()
            await asyncio.sleep(1)

    def _start_sdk_thread(self) -> None:
        self._thread = threading.Thread(target=self._run_sdk_in_thread, daemon=True, name="feishu-ws-sdk")
        self._thread.start()
        logger.info("[Feishu WS] SDK 客户端已启动")

    def _run_sdk_in_thread(self) -> None:
        import lark_oapi as lark
        import lark_oapi.ws.client as ws_client

        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        ws_client.loop = new_loop

        _noop = lambda _: None
        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_sdk_message_received) \
            .register_p2_customized_event("im.message.reaction.created_v1", _noop) \
            .register_p2_customized_event("im.message.reaction.deleted_v1", _noop) \
            .register_p2_customized_event("im.message.message_read_v1", _noop) \
            .register_p2_customized_event("im.chat.access_event.bot_p2p_chat_entered_v1", _noop) \
            .build()

        client = lark.ws.Client(
            os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"],
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )
        client.on_reconnected = lambda: logger.info("[Feishu WS] 飞书长连接已建立（含重连）")
        logger.info("[Feishu WS] 正在连接飞书 WebSocket ...")
        client.start()

    # ---- SDK 事件回调 ----

    def _on_sdk_message_received(self, data) -> None:
        try:
            event_data = self._convert_sdk_event_to_dict(data)
            if event_data:
                msg = event_data["event"]["message"]
                logger.info(f"[Feishu WS] 收到消息 chat_type={msg.get('chat_type')} chat_id={msg.get('chat_id')}")
                threading.Thread(target=self._process_event, args=(event_data,), daemon=True).start()
        except Exception:
            logger.exception("[Feishu WS] SDK 事件回调异常")

    @staticmethod
    def _convert_sdk_event_to_dict(data) -> Optional[Dict[str, Any]]:
        if not data.event or not data.event.message:
            return None
        msg = data.event.message
        mentions = []
        if msg.mentions:
            for m in msg.mentions:
                mention_dict: Dict[str, Any] = {"key": m.key or "", "name": m.name or "", "tenant_key": m.tenant_key or ""}
                if m.id:
                    mid: Dict[str, str] = {}
                    for attr in ("user_id", "open_id", "union_id"):
                        val = getattr(m.id, attr, None)
                        if val:
                            mid[attr] = val
                    if mid:
                        mention_dict["id"] = mid
                mentions.append(mention_dict)
        return {
            "header": {"event_type": data.header.event_type if data.header else "im.message.receive_v1"},
            "event": {
                "message": {
                    "chat_id": msg.chat_id or "",
                    "chat_type": msg.chat_type or "",
                    "message_id": msg.message_id or "",
                    "message_type": msg.message_type or "",
                    "content": msg.content or "",
                    "mentions": mentions,
                }
            },
        }

    # ---- 事件处理 ----

    def _process_event(self, event_data: dict) -> None:
        header = event_data.get("header", {})
        event = event_data.get("event", {})
        event_type = header.get("event_type", "")
        if event_type != "im.message.receive_v1":
            return

        message = event.get("message", {}) if event else {}
        chat_id = (message.get("chat_id") or "").strip()
        chat_type = (message.get("chat_type") or "").strip()
        message_id = (message.get("message_id") or message.get("id") or "").strip()
        message_type = (message.get("message_type") or message.get("msg_type") or "").strip()

        if not chat_id:
            return

        logger.info(f"[Feishu WS] 收到消息 chat_id={chat_id} type={message_type}")

        if message_type == "image":
            keys = extract_image_keys_from_event_message(message)
            if keys and message_id:
                self._cache_image_keys(chat_id, message_id, keys)
            return

        text = extract_text_from_event_message(message)
        if not text:
            return

        if chat_type == "group":
            mentions = message.get("mentions") or []
            if not mentions:
                logger.info(f"[Feishu WS] 群聊未@机器人，跳过")
                return

        if text.startswith("/img"):
            self._handle_img_command(chat_id, text)
            return

        cached = self._consume_image_cache(chat_id)
        self._run_agent_and_reply(chat_id, text, cached)

    # ---- 图片缓存 ----

    def _cache_image_keys(self, chat_id: str, message_id: str, keys: List[str]) -> None:
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
        logger.info(f"[Feishu WS] 已缓存图片 chat_id={chat_id} count={len(keys)}")

    def _consume_image_cache(self, chat_id: str) -> List[Dict[str, str]]:
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

    # ---- /img 命令 ----

    def _handle_img_command(self, chat_id: str, text: str) -> None:
        parts = [p for p in text.split() if p.strip()]
        image_ids = [p.replace("/agent-images/", "", 1) if p.startswith("/agent-images/") else p for p in parts[1:]]
        image_ids = [x for x in image_ids if x]

        if not image_ids:
            send_text_message_to_chat(chat_id, "未提供图片ID。用法：/img img-xxx img-yyy")
            return

        images_data = self.agent.db.get_agent_images_batch(image_ids) or []
        b64_list = []
        for row in images_data:
            if isinstance(row, dict) and row.get("base64"):
                b64_list.append(row["base64"])

        logger.info(f"[Feishu WS] /img 取图 chat_id={chat_id} requested={len(image_ids)} found={len(b64_list)}")
        if not b64_list:
            send_text_message_to_chat(chat_id, "没有在DB里找到对应图片。")
            return

        r = send_images_base64_to_chat(chat_id, b64_list)
        if not r.get("ok"):
            send_text_message_to_chat(chat_id, f"回发图片部分失败：sent={r.get('sent')} failed={r.get('failed')}")

    # ---- Agent 处理并回复 ----

    @staticmethod
    def _extract_img_ids_from_text(s: str) -> List[str]:
        ids = re.findall(r"\bimg-[0-9a-fA-F-]{6,}\b", s or "")
        seen = set()
        out = []
        for i in ids:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out[:20]

    @staticmethod
    def _format_ask_user_text(d: Dict[str, Any]) -> str:
        if not isinstance(d, dict):
            return "需要你补充信息，请直接回复。"
        question = (d.get("question") or "需要你补充信息，请确认").strip()
        options = d.get("options") or []
        lines = [question]
        if isinstance(options, list) and options:
            for i, opt in enumerate(options[:20], 1):
                if isinstance(opt, dict):
                    opt_type = (opt.get("type") or "text").strip()
                    opt_content = (opt.get("content") or "").strip()
                    if opt_type == "text":
                        lines.append(f"{i}. {opt_content or '（空）'}")
                    else:
                        lines.append(f"{i}. [{opt_type}] {opt_content or '（空）'}")
                else:
                    lines.append(f"{i}. {str(opt)}")
            lines.append("请直接回复选项内容或序号。")
        return "\n".join(lines).strip()

    def _run_agent_and_reply(
        self,
        chat_id: str,
        text: str,
        cached_resources: List[Dict[str, str]],
    ) -> None:
        try:
            images_base64: List[str] = []
            if cached_resources:
                for r in cached_resources:
                    mid = (r or {}).get("message_id") or ""
                    fk = (r or {}).get("file_key") or ""
                    dl = download_message_resource(mid, fk, resource_type="image")
                    if not dl:
                        continue
                    img_bytes, content_type = dl
                    ext = "png"
                    if content_type and "image/" in content_type:
                        ext = content_type.split(";")[0].split("/")[-1].strip() or "png"
                    if ext == "jpg":
                        ext = "jpeg"
                    mime = f"image/{ext}" if ext in ("png", "jpeg", "gif", "webp") else "image/png"
                    b64 = f"data:{mime};base64,{__import__('base64').b64encode(img_bytes).decode('ascii')}"
                    if b64:
                        images_base64.append(b64)
                        logger.info(f"[Feishu WS] 已下载图片 mid={mid[:8]} fk={fk[:12]} size={len(img_bytes)}")
                    else:
                        logger.error(f"[Feishu WS] 图片转base64失败 mid={mid[:8]} fk={fk[:12]}")

            session_id = None
            chat_row = self.agent.db.get_chat_session(chat_id)
            if chat_row:
                session_id = chat_row.get("session_id")
                existing = self.agent.db.get_agent_session(session_id)
                if not existing:
                    session_id = None
                    self.agent.db.delete_chat_session(chat_id)
            if not session_id:
                session_id = self.agent.db.create_agent_session(f"feishu:{chat_id[:8]}")
                self.agent.db.set_chat_session(chat_id, session_id)

            config = AgentConfig(
                base_url=self._base_url,
                sys_prompt=self._sys_prompt,
                api_spec=self._api_spec,
                verbose=os.environ.get("FEISHU_VERBOSE", "").lower() in ("1", "true", "yes"),
            )

            reply_text = ""
            out_images_base64: List[str] = []
            for ev_type, ev_data in self.agent.chat_stream_events(
                user_message=text,
                session_id=session_id,
                config=config,
                images=images_base64 or None,
            ):
                if ev_type == "step_done":
                    imgs = ((ev_data or {}).get("result") or {}).get("images")
                    if isinstance(imgs, dict):
                        for _img_id, _b64 in imgs.items():
                            if isinstance(_b64, str) and _b64.startswith("data:image/"):
                                out_images_base64.append(_b64)
                if ev_type == "ask_user":
                    reply_text = self._format_ask_user_text(ev_data or {})
                    break
                if ev_type == "error":
                    reply_text = f"（Agent 出错：{((ev_data or {}).get('error') or 'unknown_error')}）"
                    break
                if ev_type == "done":
                    reply_text = (((ev_data or {}).get("reply")) or "").strip()
                    break

            if not reply_text:
                reply_text = "（Agent 未返回有效回复）"

            logger.info(f"[Feishu WS] Agent回复 chat_id={chat_id}: {reply_text[:300]}")
            ok = send_markdown_message_to_chat(chat_id, reply_text)
            if not ok:
                logger.error("[Feishu WS] 回发消息失败")

            img_ids = self._extract_img_ids_from_text(reply_text)
            if img_ids:
                images_data = self.agent.db.get_agent_images_batch(img_ids) or []
                for row in images_data:
                    if isinstance(row, dict) and row.get("base64"):
                        out_images_base64.append(row["base64"])

            if out_images_base64:
                r = send_images_base64_to_chat(chat_id, out_images_base64[:10])
                logger.info(f"[Feishu WS] 自动回发图片 chat_id={chat_id} sent={r.get('sent')} failed={r.get('failed')}")

        except Exception:
            logger.exception(f"[Feishu WS] 处理消息失败 chat_id={chat_id}")
