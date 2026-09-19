# -*- coding: utf-8 -*-
"""list_endpoints —— 让 agent 看到「有哪些端点可发」（P4）。

模型自由路由的前提：**先看得见有哪些目标**（通道管理给的「菜单」）。
返回：端点列表（地址 / 类别 / 状态 / 能力）+ 各通道概览。
"""

from __future__ import annotations

from typing import Any, Dict

from ..models import ToolResult

LIST_ENDPOINTS_DEF: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "channel": {"type": "string", "description": "按通道类型过滤，如 feishu / event"},
        "kind": {"type": "string",
                 "description": "按端点类别过滤：user / group / service / agent / event / task"},
        "q": {"type": "string", "description": "关键字（匹配地址 / 名称）"},
    },
}


def list_endpoints(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    from agent_core.channel.admin import channel_overview, endpoints_snapshot

    snap = endpoints_snapshot(
        channel=args.get("channel"),
        kind=args.get("kind"),
        q=args.get("q"),
    )
    snap["channels"] = channel_overview()
    return ToolResult(ok=True, data=snap)


__all__ = ["LIST_ENDPOINTS_DEF", "list_endpoints"]
