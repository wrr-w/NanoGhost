# -*- coding: utf-8 -*-
"""Agent 事件流展示层（Presenter）。

把 Agent.chat_stream_events() 产生的事件流翻译并渲染为渠道消息，
通过 ChannelIO 接口发送给用户。

完全与渠道无关——不直接调用飞书或任何平台的 API。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from agent_core.engine.agent import Agent
from agent_core.config import AgentConfig
from agent_core.channel.instance import BotInstance
from agent_core.channel.session import SessionStore
from agent_core.channel.interfaces import ChannelIO
from agent_core.channel.message_context import ContextBuilder, MessageSource, MessageContext

logger = logging.getLogger("agent_core")

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
    io: ChannelIO,
    context_builder: ContextBuilder,
    source: MessageSource,
    ctx: MessageContext,
    user_text: str,
    images_base64: Optional[List[str]] = None,
    base_url: str = "",
    api_spec: Optional[Dict] = None,
) -> str:
    """执行一轮 Agent 对话。

    Args:
        agent: Agent 实例
        identity: Bot 实例信息
        sessions: Session 管理器
        io: 渠道 I/O 实现
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

    config = AgentConfig(
        base_url=base_url,
        sys_prompt=full_sys_prompt,
        api_spec=api_spec or {},
        history_max_messages=getattr(identity, "history_max_messages", 120),
        history_max_tokens=getattr(identity, "history_max_tokens", 200_000),
        root_id=ctx.root_id or None,
    )

    # 4. Reaction 表示正在处理
    reaction_id = ""
    if message_id:
        reaction_id = io.add_reaction(message_id)

    try:
        # 5. Agent 执行
        reply_text = ""
        out_images: List[str] = []
        _t_start = time.time()
        feedback_level = identity.get_feedback_level()
        text_stream_content = ""
        done_sent = False

        async for ev_type, ev_data in agent.chat_stream_events(
            user_message=user_text,
            session_id=session_id,
            config=config,
            images=images_base64 or None,
        ):
            if ev_type == "text_stream" and feedback_level >= 2:
                text_stream_content = ((ev_data or {}).get("content") or "").strip()

            if ev_type == "tool_call" and feedback_level >= 3:
                if text_stream_content:
                    io.send_text(chat_id, text_stream_content)
                    text_stream_content = ""
                name = ((ev_data or {}).get("name") or "").strip()
                preview = ((ev_data or {}).get("preview") or "").strip()
                if name:
                    emoji = _tool_emoji(name)
                    label = _tool_label(name)
                    msg = f"{emoji} {label}: {preview}" if preview else f"{emoji} {name}..."
                    io.send_text(chat_id, msg)

            if ev_type == "tool_result" and feedback_level >= 4:
                ok = (ev_data or {}).get("ok", True)
                summary = ((ev_data or {}).get("summary") or "").strip()
                if ok and summary:
                    io.send_text(chat_id, f"  {summary[:200]}")

            if ev_type == "step_done":
                imgs = ((ev_data or {}).get("result") or {}).get("images")
                if isinstance(imgs, dict):
                    for _img_id, _b64 in imgs.items():
                        if isinstance(_b64, str) and _b64.startswith("data:image/"):
                            out_images.append(_b64)

            if ev_type == "ask_user":
                reply_text = _format_ask_user_text(ev_data or {})
                break

            if ev_type == "error":
                reply_text = f"(Agent error: {((ev_data or {}).get('error') or 'unknown')})"
                break

            if ev_type == "done":
                text_stream_content = ""
                reply_text = (((ev_data or {}).get("reply")) or "").strip()
                if reply_text:
                    logger.info(f"[Presenter] reply chat_id={chat_id} time={time.time()-_t_start:.0f}s")
                    if message_id:
                        io.reply(message_id, reply_text)
                    else:
                        io.send_text(chat_id, reply_text)
                    # 提取图片引用
                    img_ids = _extract_img_ids(reply_text)
                    if img_ids:
                        images_data = agent.db.get_agent_images_batch(img_ids) or []
                        for row in images_data:
                            if isinstance(row, dict) and row.get("base64"):
                                out_images.append(row["base64"])
                reply_text = "__DONE_SENT__"
                done_sent = True

        # 6. 兜底发送
        if reply_text == "__DONE_SENT__":
            reply_text = ""
        if reply_text and not done_sent:
            if message_id:
                io.reply(message_id, reply_text)
            else:
                io.send_text(chat_id, reply_text)
            img_ids = _extract_img_ids(reply_text)
            if img_ids:
                images_data = agent.db.get_agent_images_batch(img_ids) or []
                for row in images_data:
                    if isinstance(row, dict) and row.get("base64"):
                        out_images.append(row["base64"])

        # 7. 回发图片
        if out_images:
            io.send_images(chat_id, out_images[:10])

        return reply_text

    finally:
        if message_id and reaction_id:
            io.delete_reaction(message_id, reaction_id)


def _extract_img_ids(s: str) -> List[str]:
    import re
    ids = re.findall(r"\bimg-[0-9a-fA-F-]{6,}\b", s or "")
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out[:20]


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
