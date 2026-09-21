# -*- coding: utf-8 -*-
"""Agent 事件流展示层（Presenter）。

把 Agent.chat_stream_events() 产生的事件流翻译并渲染为渠道消息，
通过 ChannelIO 接口发送给用户。

完全与渠道无关——不直接调用飞书或任何平台的 API。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from agent_core.engine.agent import Agent
from agent_core.channel.base import Channel
from agent_core.config import AgentConfig
from agent_core.channel.instance import BotInstance
from agent_core.channel.responder import TurnResponder
from agent_core.channel.route import make_image_block, make_markdown_block, make_text_block
from agent_core.channel.session import SessionStore
from agent_core.channel.message_context import ContextBuilder, MessageSource, MessageContext
from agent_core.memory.files import read_daily_memory_block
from agent_core.router import get_router

logger = logging.getLogger("agent_core")


class _CompatIOChannel(Channel):
    def __init__(self, name: str, io) -> None:
        self.name = name
        self.io = io

    def send(self, target: str, text: str) -> bool:
        return bool(self.io.send_text(target, text))

    def reply(self, message_id: str, text: str) -> bool:
        return bool(self.io.reply(message_id, text))

    def send_images(self, target: str, b64_list: List[str]):
        return self.io.send_images(target, b64_list)

    def add_reaction(self, message_id: str) -> str:
        return self.io.add_reaction(message_id)

    def delete_reaction(self, message_id: str, reaction_id: str) -> bool:
        return bool(self.io.delete_reaction(message_id, reaction_id))

    def capabilities(self):
        return {"text", "image", "reaction"}

    def default_delivery_policy(self) -> dict:
        return {
            "supports_reply": True,
            "default_allow_reply": True,
            "default_prefer_reply": True,
        }


def _ensure_router_channel(router, platform: str, io) -> None:
    if io is None:
        return
    reg = router._registry()
    current = reg.get(platform)
    if current is not None and getattr(current, "io", None) is io:
        return
    reg.register(_CompatIOChannel(platform, io))

# ── 工具反馈 emoji & 标签映射 ──
_TOOL_EMOJI = {
    "web_search": "\U0001f50d", "search_web": "\U0001f50d",
    "terminal": "\U0001f5a5\ufe0f", "read": "\U0001f4c4",
    "ask_user": "\u2753",
    "send_message": "\U0001f4ac",
    "skills_list": "\U0001f9f0", "use_skill": "\U0001f9f0", "skill_manage": "\U0001f9f0",
    "memory_write": "\U0001f4be",
    "delegate_task": "\U0001f916",
}

def _tool_emoji(name: str) -> str:
    return _TOOL_EMOJI.get(name, "\U0001f504")

def _tool_label(name: str) -> str:
    n = name.lower()
    for kws, label in [
        (("search", "web_"), "\u641c\u7d22"),
        (("create", "add", "new"), "\u521b\u5efa"),
        (("list", "get_", "query", "find"), "\u67e5\u8be2"),
        (("start", "run", "launch"), "\u542f\u52a8"),
        (("stop", "pause", "cancel", "delete", "remove"), "\u505c\u6b62"),
        (("update", "edit", "modify", "set", "change"), "\u66f4\u65b0"),
        (("read", "open"), "\u8bfb\u53d6"),
        (("send", "reply", "post"), "\u53d1\u9001"),
        (("delegate", "subagent"), "\u59d4\u6258"),
    ]:
        if any(kw in n for kw in kws):
            return label
    return "\u6267\u884c"


async def run_agent_turn(
    *,
    agent: Agent,
    identity: BotInstance,
    sessions: SessionStore,
    io=None,
    context_builder: ContextBuilder,
    source: MessageSource,
    ctx: MessageContext,
    user_text: str,
    images_base64: Optional[List[str]] = None,
    base_url: str = "",
    api_spec: Optional[Dict] = None,
    channel_ctx: Optional[Dict] = None,
) -> str:
    """执行一轮 Agent 对话。

    Args:
        agent: Agent 实例
        identity: Bot 实例信息
        sessions: Session 管理器
        io: 兼容保留参数，当前不再用于出站
        source: 消息来源
        ctx: 消息内容
        user_text: 已经过 ContextBuilder 格式化的用户文本
        images_base64: 图片 base64 列表
        base_url, api_spec: LLM 配置

    Returns:
        最终回复文本
    """
    chat_id = source.chat_id
    message_id = ctx.message_id

    # 1. 刷新记忆
    identity.refresh_memory()

    # 2. Session
    session_id, is_new = sessions.get_or_create(chat_id)

    # 3. System prompt（实例 + session 上下文）
    full_sys_prompt = identity.get_base_sys_prompt()
    session_context = sessions.get_context_block(source)
    if is_new or session_context not in full_sys_prompt:
        full_sys_prompt += "\n\n" + session_context

    today_str = date.today().isoformat()
    daily_memory = read_daily_memory_block(os.environ.get("INSTANCE_DIR", ""), today_str)
    extra_system_blocks: List[Dict[str, Any]] = []
    if daily_memory:
        extra_system_blocks.append(
            {
                "role": "system",
                "content": [{"type": "text", "text": f"## 今日短期记忆\n\n{daily_memory}"}],
            }
        )

    root_key = ""
    if source.thread_id:
        root_key = source.thread_id
    elif ctx.root_id:
        root_key = ctx.root_id

    config = AgentConfig(
        base_url=base_url,
        sys_prompt=full_sys_prompt,
        api_spec=api_spec or {},
        extra_system_messages=extra_system_blocks,
        history_max_messages=getattr(identity, "history_max_messages", 120),
        history_max_tokens=getattr(identity, "history_max_tokens", 200_000),
        root_id=root_key or None,
    )

    # 4. Reaction 表示正在处理
    reaction_id = ""
    router = get_router()
    platform = getattr(source, "platform", "feishu")
    _ensure_router_channel(router, platform, io)
    source_addr = f"{platform}:{chat_id}"
    if message_id:
        reaction_id = router.add_reaction(source_addr, message_id)

    responder = TurnResponder(
        router=router,
        platform=platform,
        chat_id=chat_id,
        message_id=message_id,
        namespace=getattr(agent, "namespace", "default") or "default",
    )

    try:
        # 5. Agent 执行
        reply_text = ""
        out_images: List[str] = []
        _t_start = time.time()
        feedback_level = identity.get_feedback_level()
        text_stream_content = ""
        done_sent = False

        stream_kwargs = {
            "user_message": user_text,
            "session_id": session_id,
            "config": config,
            "images": images_base64 or None,
        }
        try:
            params = inspect.signature(agent.chat_stream_events).parameters
            if "channel_ctx" in params:
                stream_kwargs["channel_ctx"] = channel_ctx or {
                    "chat_id": chat_id,
                    "platform": getattr(source, "platform", "feishu"),
                }
        except (TypeError, ValueError):
            pass

        async for ev_type, ev_data in agent.chat_stream_events(**stream_kwargs):
            if ev_type == "text_stream" and feedback_level >= 2:
                text_stream_content = ((ev_data or {}).get("content") or "").strip()

            if ev_type == "tool_call" and feedback_level >= 3:
                if text_stream_content:
                    responder.emit([make_text_block(text_stream_content)], delivery="send")
                    text_stream_content = ""
                name = ((ev_data or {}).get("name") or "").strip()
                preview = ((ev_data or {}).get("preview") or "").strip()
                if name:
                    emoji = _tool_emoji(name)
                    label = _tool_label(name)
                    msg = f"{emoji} {label}: {preview}" if preview else f"{emoji} {name}..."
                    responder.emit([make_text_block(msg)], delivery="send")

            if ev_type == "tool_result" and feedback_level >= 4:
                ok = (ev_data or {}).get("ok", True)
                summary = ((ev_data or {}).get("summary") or "").strip()
                if ok and summary:
                    responder.emit([make_text_block(f"  {summary[:200]}")], delivery="send")

            if ev_type == "step_done":
                imgs = ((ev_data or {}).get("result") or {}).get("images")
                if isinstance(imgs, dict):
                    for _img_id, _b64 in imgs.items():
                        if isinstance(_b64, str) and _b64.startswith("data:image/"):
                            out_images.append(_b64)

            if ev_type == "ask_user":
                ask_text = _format_ask_user_text(ev_data or {})
                prefix = text_stream_content.strip()
                text_stream_content = ""
                reply_text = f"{prefix}\n\n{ask_text}" if prefix else ask_text
                break

            if ev_type == "error":
                prefix = text_stream_content.strip()
                text_stream_content = ""
                err = f"(Agent error: {((ev_data or {}).get('error') or 'unknown')})"
                reply_text = f"{prefix}\n\n{err}" if prefix else err
                break

            if ev_type == "done":
                text_stream_content = ""
                reply_text = (((ev_data or {}).get("reply")) or "").strip()
                if reply_text:
                    logger.info(f"[Presenter] reply chat_id={chat_id} time={time.time()-_t_start:.0f}s")
                    responder.emit([make_markdown_block(reply_text)])
                reply_text = "__DONE_SENT__"
                done_sent = True

        # 6. 兜底发送
        if reply_text == "__DONE_SENT__":
            reply_text = ""
        if reply_text and not done_sent:
            responder.emit([make_markdown_block(reply_text)])

        # 7. 回发图片
        if out_images:
            responder.emit([make_image_block(out_images)], delivery="send")

        return reply_text

    finally:
        if message_id and reaction_id:
            router.delete_reaction(source_addr, message_id, reaction_id)


def _format_ask_user_text(d: Dict[str, Any]) -> str:
    if not isinstance(d, dict):
        return "Ask user for clarification."
    question = (d.get("question") or "Need your input").strip()
    options = d.get("options") or []
    lines = [question]
    if isinstance(options, list) and options:
        for i, opt in enumerate(options[:20], 1):
            if isinstance(opt, dict):
                ot = (opt.get("type") or "text").strip()
                oc = (opt.get("content") or "").strip()
                lines.append(f"{i}. {oc or '(empty)'}" if ot == "text" else f"{i}. [{ot}] {oc or '(empty)'}")
            else:
                lines.append(f"{i}. {str(opt)}")
        lines.append("Reply with option number or content.")
    return "\n".join(lines).strip()
