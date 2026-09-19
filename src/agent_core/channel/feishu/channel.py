# -*- coding: utf-8 -*-
"""FeishuChannel —— 把飞书套成统一 Channel（P0）。

只做「适配」：把 Channel 的 send / reply / update 桥到既有 FeishuIO / api。
**不改 FeishuIO 本体**，旧路径完全不受影响。

飞书地址约定：target = chat_id
    feishu:oc_xxx   群
    feishu:<p2p chat_id>   与某人的私聊
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from agent_core.channel.base import Channel

from .io import FeishuIO


class FeishuChannel(Channel):
    """飞书通道适配器。"""

    name = "feishu"

    def __init__(self, io: Optional[FeishuIO] = None) -> None:
        # 复用既有的 I/O 实现（含 @昵称→<at> 反查）
        self.io = io or FeishuIO()

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

    def add_reaction(self, message_id: str) -> str:
        return self.io.add_reaction(message_id)

    def delete_reaction(self, message_id: str, reaction_id: str) -> bool:
        return self.io.delete_reaction(message_id, reaction_id)

    # ── 能力 ──
    def capabilities(self) -> Set[str]:
        return {"text", "markdown", "mention", "image", "reaction"}

    # ── 端点地址 ──
    @staticmethod
    def make_addr(chat_id: str) -> str:
        return f"feishu:{chat_id}"


__all__ = ["FeishuChannel"]
