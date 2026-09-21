# -*- coding: utf-8 -*-
"""FeishuChannel —— 飞书渠道的**完整适配器**（接收 + 归一 + 发送）。

乙b（组合）：FeishuChannel 是对外唯一门面
    ├─ io        FeishuIO            出站原语
    ├─ turn      FeishuTurnParser    入站归一（parse_inbound）
    ├─ transport FeishuTransport     接收管道（WS 长连接 + 每端点收件箱消费）
    └─ 会话编排  handle_message / on_subagent_done / mirror_outbound

内核（Router / presenter / Agent）从不碰飞书格式：
    · 入站：飞书原始事件 → MessageSource/Context（本渠道解析）
    · 出站：blocks → 飞书消息（本渠道发送）

飞书地址约定：target = chat_id
    feishu:oc_xxx           群
    feishu:<p2p chat_id>    与某人的私聊
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Set

from agent_core.channel.base import Channel
from agent_core.channel.directory import register_endpoint
from agent_core.channel.endpoint import parse_addr
from agent_core.channel.instance import BotInstance
from agent_core.channel.message_context import ContextBuilder
from agent_core.channel.registry import register_channel
from agent_core.channel.session import SessionStore
from agent_core.presenter import run_agent_turn
from agent_core.router import get_router

from . import api
from .io import FeishuIO
from .tools import register_feishu_tools
from .turn import FeishuTurnParser

logger = logging.getLogger("agent_core")


class FeishuChannel(Channel):
    """飞书渠道适配器（接收 + 归一 + 发送）。"""

    name = "feishu"

    def __init__(
        self,
        *,
        io: Optional[FeishuIO] = None,
        agent: Any = None,
        sys_prompt: str = "",
        api_spec: Optional[Dict] = None,
        base_url: str = "",
        log: Any = None,
    ) -> None:
        self.log = log or logger
        # 复用既有的 I/O 实现（含 @昵称→<at> 反查）
        self.io = io or FeishuIO()
        self.agent = agent
        self.instance = None
        self.sessions = None
        self._base_url = (base_url or os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:8000")).rstrip("/")
        self._api_spec = api_spec or {}

        # 通用上下文 + 入站解析器（归一入口；纯 io 场景也建好）
        bot_name = os.environ.get("FEISHU_BOT_NAME", "")
        bot_id = os.environ.get("FEISHU_BOT_OPEN_ID", "")
        self.context_builder = ContextBuilder(
            bot_name=bot_name, bot_id=bot_id,
            lark_cli_profile=os.environ.get("LARK_CLI_PROFILE", ""),
        )
        self._context_builder = self.context_builder      # 兼容旧写法
        self.turn = FeishuTurnParser(self.context_builder)
        self.transport = None
        self.sdk = None

        if agent is not None:
            self._wire_agent(agent, sys_prompt or "", bot_name, bot_id)

    def _wire_agent(self, agent: Any, sys_prompt: str, bot_name: str, bot_id: str) -> None:
        """有 agent 时才需要「接收 + 会话」——组装接收管道并接线。"""
        self.instance = BotInstance(sys_prompt, bot_name=bot_name, bot_id=bot_id)
        self.instance.load_feedback_level()
        self.instance.load_history_limits()
        self.sessions = SessionStore(agent, self.context_builder, get_bot_id=lambda: self.instance.bot_id)
        self.io.set_name_map(self.sessions.mention_name_map)

        try:
            register_channel(self)
        except Exception:
            logger.exception("[Feishu] register channel failed")

        try:
            get_router().set_mirror(self.mirror_outbound)
        except Exception:
            logger.exception("[Feishu] set router mirror failed")

        try:
            register_feishu_tools(self.agent, self.sessions.mention_name_map)
        except Exception:
            logger.exception("[Feishu] register feishu tools failed")

        # 接收管道（组合）：WS + 每端点收件箱消费
        from .transport import FeishuTransport
        self.transport = FeishuTransport(self)
        self.sdk = self.transport.sdk

    # ── 出站 ──
    def send(self, target: str, text: str) -> bool:
        return self.io.send_text(target, text)

    def reply(self, message_id: str, text: str) -> bool:
        return self.io.reply(message_id, text)

    def update(self, message_id: str, text: str) -> bool:
        # 飞书卡片原地更新需 card API；P0 暂不实现（留到通道管理阶段）
        return False

    # ── 扩展 I/O（飞书有、通用 Channel 没有的），供需要时直接调 ──
    def send_images(self, target: str, b64_list: List[str]) -> Dict[str, Any]:
        return self.io.send_images(target, b64_list)

    def send_files(self, target: str, files: List[Any]) -> Dict[str, Any]:
        """发送本机文件（允许任意目录）。返回 {ok, sent, failed, errors}。"""
        return self.io.send_files(target, files)

    def add_reaction(self, message_id: str) -> str:
        return self.io.add_reaction(message_id)

    def delete_reaction(self, message_id: str, reaction_id: str) -> bool:
        return self.io.delete_reaction(message_id, reaction_id)

    # ── 能力 ──
    def capabilities(self) -> Set[str]:
        return {"text", "markdown", "mention", "image", "file", "reaction"}

    def message_capability_profile(self) -> Dict[str, Any]:
        return {
            "channel": self.name,
            "delivery": ["reply", "send"],
            "block_types": ["text", "image", "markdown", "file"],
            "supports_multi_block": True,
            "supports_mixed_blocks": True,
            "supports_file": True,
            "supports_card": False,
            "supports_mentions": True,
            "supports_reply": True,
            "fallbacks": {
                "markdown": "text",
            },
            "limits": {
                "max_blocks": 10,
                "max_images_per_block": 10,
                "max_files_per_block": 10,
            },
        }

    def default_delivery_policy(self) -> dict:
        return {
            "supports_reply": True,
            "default_allow_reply": True,
            "default_prefer_reply": True,
        }

    def send_blocks(self, target: str, blocks: List[Dict[str, Any]], *, delivery: str = "send", reply_to: str | None = None) -> bool:
        used_reply = False
        for block in list(blocks or []):
            btype = str(block.get("type") or "").strip().lower()
            if btype in ("text", "markdown"):
                text = str(block.get("text") or "")
                if not text.strip():
                    continue
                if delivery == "reply" and reply_to and not used_reply:
                    if btype == "markdown":
                        ok = bool(self.io.reply_markdown(reply_to, text))
                    else:
                        ok = bool(self.reply(reply_to, text))
                    used_reply = True
                else:
                    if btype == "markdown":
                        ok = bool(self.io.send_markdown(target, text))
                    else:
                        ok = bool(self.send(target, text))
            elif btype == "image":
                images = [str(x) for x in list(block.get("images") or []) if str(x)]
                if not images:
                    continue
                self.send_images(target, images[:10])
                ok = True
            elif btype == "file":
                files = list(block.get("files") or [])
                if not files:
                    continue
                res = self.send_files(target, files)
                ok = bool(res.get("sent")) if isinstance(res, dict) else bool(res)
                if not ok:
                    logger.warning("[Feishu] 文件发送未成功，降级为文本: %s", res)
                    lines = []
                    for f in files:
                        if isinstance(f, dict):
                            lines.append(f"📎 {f.get('name') or ''} ({f.get('path') or ''})")
                        else:
                            lines.append(f"📎 {f}")
                    if lines:
                        self.send(target, "\n".join(lines))
                        ok = True
            else:
                return False
            if not ok:
                return False
        return True

    # ── 端点地址 ──
    @staticmethod
    def make_addr(chat_id: str) -> str:
        return f"feishu:{chat_id}"

    # ════════════════════════════════════════════
    # 入站归一（Channel 契约）
    # ════════════════════════════════════════════

    def parse_inbound(self, payload: Any):
        """飞书原始事件 → 通用 (MessageSource, MessageContext)。

        渠道的固有职责；内核（Router/presenter/Agent）从不碰飞书格式。
        """
        return self.turn.parse_event(payload)

    # ════════════════════════════════════════════
    # 生命周期（委托接收管道）
    # ════════════════════════════════════════════

    async def start(self) -> None:
        if self.transport is not None:
            await self.transport.start()

    async def stop(self) -> None:
        if self.transport is not None:
            await self.transport.stop()

    async def run_forever(self) -> None:
        if self.transport is not None:
            await self.transport.run_forever()

    def _ensure_bot_id(self) -> None:
        if self.transport is not None:
            self.transport._ensure_bot_id()

    # ════════════════════════════════════════════
    # 会话编排（由接收管道回调）
    # ════════════════════════════════════════════

    async def handle_message(self, event_data: dict) -> None:
        """编排一条消息的完整生命周期（原 ws_client._process）。"""
        if event_data.get("header", {}).get("event_type", "") != "im.message.receive_v1":
            logger.info("[Feishu] SKIP event_type != im.message.receive_v1")
            return

        # 1. 图片→缓存
        if self.turn.is_image_message(event_data):
            message = event_data["event"]["message"]
            keys = self.turn._get_image_keys(event_data)
            if keys and message.get("message_id"):
                self.sessions.cache_image_keys(
                    message["chat_id"], message["message_id"], keys
                )
            logger.info(f"[Feishu] IMAGE cached keys={keys}")
            return

        # 2. 群聊未@bot→跳过
        is_mention = self.turn.is_group_mention_bot(event_data, self.instance.bot_id or "", self.instance.bot_name or "")
        logger.info(f"[Feishu] is_group_mention_bot={is_mention}")
        if not is_mention:
            logger.info("[Feishu] SKIP not mention bot")
            return

        # 3. 解析（渠道归一）
        source, ctx = self.parse_inbound(event_data)
        logger.info(
            f"[Feishu] PARSED sender_id={source.sender_id} "
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
            logger.info("[Feishu] SKIP message sent by self")
            return

        # 3.7 端点动态注册（P0）：收到消息即登记「这个会话端点」
        try:
            kind = "group" if source.is_group else "user"
            register_endpoint(
                f"feishu:{source.chat_id}",
                channel="feishu",
                kind=kind,
                capabilities=self.capabilities(),
                meta={
                    "chat_id": source.chat_id,
                    "chat_name": source.chat_name,
                    "chat_type": source.chat_type,
                    "sender_id": source.sender_id,
                    "sender_name": source.sender_name,
                },
            )
        except Exception:
            logger.exception("[Feishu] register endpoint failed")

        if not ctx.text and not ctx.mentions:
            logger.info("[Feishu] SKIP empty text and no mentions")
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
        logger.info(f"[Feishu] image_cache consumed={len(cached)}")

        # 6. 下载图片→base64
        images_base64 = []
        if cached:
            import base64
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
                b64 = f"data:{mime};base64,{base64.b64encode(img_bytes).decode('ascii')}"
                if b64:
                    images_base64.append(b64)

        # 7. 格式化用户文本
        user_text = self._context_builder.build_user_message(source, ctx)
        logger.info(f"[Feishu] USER_TEXT for LLM={user_text[:200]}")

        # 8. Agent 执行（通用循环）
        session_id, is_new = self.sessions.get_or_create(chat_id)
        logger.info(f"[Feishu] SESSION session_id={session_id} is_new={is_new}")

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

    async def on_subagent_done(self, target: str, payload: Dict[str, Any]) -> None:
        """后台子任务完成（P3）：父 agent 空闲时，起一轮把完成结果告知它。"""
        desc = payload.get("description") or ""
        status = payload.get("status") or ""
        if status == "done":
            body = f"[后台子任务完成] 「{desc}」已完成：\n{(payload.get('result') or '')[:2000]}"
        else:
            body = f"[后台子任务失败] 「{desc}」执行失败：{payload.get('error')}"
        _, chat_id = parse_addr(target)
        logger.info("[Feishu] subagent_done → 起一轮告知 chat_id=%s run_id=%s", chat_id, payload.get("run_id"))
        from agent_core.channel.message_context import MessageSource, MessageContext
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

    def mirror_outbound(self, addr: str, text: str) -> None:
        """出站镜像（P2）：把主动发到「别的端点」的消息写进**目标会话**历史。"""
        try:
            _, chat_id = parse_addr(addr)
            if not chat_id:
                return
            session_id, _ = self.sessions.get_or_create(chat_id)
            self.agent.db.add_agent_message(session_id, "assistant", text, type="text")
            logger.info("[Feishu] mirror → %s (session=%s)", addr, session_id)
        except Exception:
            logger.exception("[Feishu] mirror outbound failed addr=%s", addr)


__all__ = ["FeishuChannel"]
