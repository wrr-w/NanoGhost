# -*- coding: utf-8 -*-
"""send_message —— 让 agent 通过统一信封主动发信。"""

from __future__ import annotations

from typing import Any, Dict, List

from agent_core.channel.route import RouteEnvelope, normalize_blocks

from ..models import ToolResult

SEND_MESSAGE_DEF: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "目标端点地址列表，支持 'current_session' 或显式地址（如 'feishu:oc_xxx'）。"
                "可填多个（一对多）。不填 = 当前会话。"
            ),
        },
        "delivery": {
            "type": "string",
            "enum": ["reply", "send"],
            "description": "reply=回复当前消息；send=普通发送。",
        },
        "blocks": {
            "type": "array",
            "description": (
                "有序消息块列表。支持 text / markdown / image / file。"
                "file 用 files 字段，填本机文件路径（允许任意目录）。"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["text", "markdown", "image", "file"],
                    },
                    "text": {"type": "string"},
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "files": {
                        "type": "array",
                        "description": (
                            "本机文件路径列表（type=file 时用）。"
                            "元素为路径字符串，或 {\"path\":\"...\",\"name\":\"...\"} 指定显示名。"
                        ),
                        "items": {"type": "string"},
                    },
                },
                "required": ["type"],
            },
        },
    },
    "required": ["blocks"],
}


def _resolve_targets(to: List[str] | None, *, platform: str, chat_id: str) -> List[str]:
    targets: List[str] = []
    for item in list(to or ["current_session"]):
        value = str(item or "").strip()
        if not value:
            continue
        if value == "current_session":
            if chat_id:
                targets.append(f"{platform}:{chat_id}")
            continue
        targets.append(value)
    return targets


def send_message(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    from agent_core.router import get_router

    channel_ctx = (ctx or {}).get("channel_ctx") or {}
    chat_id = channel_ctx.get("chat_id") or ""
    platform = channel_ctx.get("platform") or "feishu"
    message_id = channel_ctx.get("message_id") or ""
    source_addr = f"{platform}:{chat_id}" if chat_id else ""
    agent_key = (ctx or {}).get("namespace") or "default"
    delivery = str(args.get("delivery") or "reply").strip().lower() or "reply"
    if delivery not in ("reply", "send"):
        return ToolResult(ok=False, error="delivery 仅支持 reply 或 send")

    targets = _resolve_targets(args.get("to"), platform=platform, chat_id=chat_id)
    if not targets:
        return ToolResult(ok=False, error="缺少可用目标；current_session 需要 chat_id")

    blocks = normalize_blocks(args.get("blocks"))
    if not blocks:
        return ToolResult(ok=False, error="blocks 不能为空")

    report = get_router().submit(
        RouteEnvelope(
            direction="outbound",
            kind="message",
            delivery=delivery,
            to=targets,
            target_addr=(targets[0] if targets else ""),
            source_addr=source_addr,
            reply_to=(message_id or None) if delivery == "reply" else None,
            agent_key=agent_key,
            blocks=blocks,
        )
    )
    ok = bool(report.get("ok"))
    return ToolResult(ok=ok, data=report, error=(None if ok else report.get("error")))


__all__ = ["SEND_MESSAGE_DEF", "send_message"]
