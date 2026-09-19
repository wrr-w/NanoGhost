# -*- coding: utf-8 -*-
"""send_message —— 让 agent 主动把消息发到任意端点（P2）。

模型自由路由：to 可指名任意端点（0..N）；不填 = 回当前会话。
实际投递走 router（含 ACL / 防环 / 限速 / 审计 / 出站镜像）。
"""

from __future__ import annotations

from typing import Any, Dict

from ..models import ToolResult

SEND_MESSAGE_DEF: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "目标端点地址列表（'channel:target'，如 'feishu:oc_xxx' / 'feishu:ou_xxx'）。"
                "可填多个（一对多）。不填 = 回当前会话。"
            ),
        },
        "text": {"type": "string", "description": "要发送的消息正文。"},
    },
    "required": ["text"],
}


def send_message(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    from agent_core.router import get_router

    text = (args.get("text") or "").strip()
    if not text:
        return ToolResult(ok=False, error="text 不能为空")

    to = args.get("to")
    channel_ctx = (ctx or {}).get("channel_ctx") or {}
    chat_id = channel_ctx.get("chat_id") or ""
    platform = channel_ctx.get("platform") or "feishu"
    source_addr = f"{platform}:{chat_id}" if chat_id else ""
    agent_key = (ctx or {}).get("namespace") or "default"

    report = get_router().send(
        to,
        text,
        ctx={"source_addr": source_addr},
        agent_key=agent_key,
    )
    ok = bool(report.get("ok"))
    return ToolResult(ok=ok, data=report, error=(None if ok else report.get("error")))


__all__ = ["SEND_MESSAGE_DEF", "send_message"]
