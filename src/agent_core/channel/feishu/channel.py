# -*- coding: utf-8 -*-
"""FeishuChannel —— 把飞书套成统一 Channel（P0）。

只做「适配」：把 Channel 的 send / reply / update 桥到既有 FeishuIO / api。
**不改 FeishuIO 本体**，旧路径完全不受影响。

飞书地址约定：target = chat_id
    feishu:oc_xxx   群
    feishu:<p2p chat_id>   与某人的私聊
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from agent_core.channel.base import Channel

from .io import FeishuIO

logger = logging.getLogger("agent_core")


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


__all__ = ["FeishuChannel"]
