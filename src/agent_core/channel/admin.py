# -*- coding: utf-8 -*-
"""通道管理接口（P4）——注册表 / 端点目录 / 能力 / 健康 / 指标 的只读视图。

给「通道管理」提供一个可查的口子（也是模型自由路由的菜单来源）：
    · channel_overview()  : 每个通道：能力 / 端点数 / 在线数
    · endpoints_snapshot(): 端点目录快照（可按 channel/kind/q 过滤）
    · channel_metrics()   : 出站指标（sent / denied / failed，来自 router 审计）

注：ACL（按 agent 授权）与限速是「写」侧配置，在 router 上（get_router().acl / .rate）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .manager import ChannelManager, get_channel_manager


def channel_overview(manager: Optional[ChannelManager] = None) -> List[Dict[str, Any]]:
    mgr = manager or get_channel_manager()
    reg = mgr.registry
    direc = mgr.directory
    out: List[Dict[str, Any]] = []
    for name in reg.names():
        ch = reg.get(name)
        eps = direc.list(channel=name)
        caps = sorted(ch.capabilities()) if ch is not None else []
        out.append({
            "channel": name,
            "enabled": mgr.is_channel_enabled(name),
            "capabilities": caps,
            "endpoints": len(eps),
            "online": len([e for e in eps if e.status == "online"]),
        })
    return out


def endpoints_snapshot(
    channel: Optional[str] = None,
    kind: Optional[str] = None,
    q: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    manager: Optional[ChannelManager] = None,
) -> Dict[str, Any]:
    mgr = manager or get_channel_manager()
    eps = mgr.directory.list(channel=channel, kind=kind, q=q, status=status)
    return {
        "count": len(eps),
        "endpoints": [e.to_dict() for e in eps[:limit]],
    }


def channel_metrics(limit: int = 1000) -> Dict[str, int]:
    """出站指标：从 router 审计缓冲里统计（sent / denied / failed）。"""
    try:
        from agent_core.router import get_router
        recs = get_router().auditor.recent(limit)
    except Exception:
        recs = []
    sent = sum(1 for r in recs if r.get("action") == "sent")
    denied = sum(1 for r in recs if str(r.get("action", "")).startswith("denied"))
    failed = sum(1 for r in recs if str(r.get("action", "")).startswith("failed"))
    return {"sent": sent, "denied": denied, "failed": failed, "records": len(recs)}


__all__ = ["channel_overview", "endpoints_snapshot", "channel_metrics"]
