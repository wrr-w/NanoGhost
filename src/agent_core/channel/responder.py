# -*- coding: utf-8 -*-
"""当前会话输出边界（回合输出的唯一门面）。

统一形式：**一个发送原语 `emit(blocks, ...)`**。
  · emit(blocks, delivery="reply")：回合回复（默认 · 回原消息）
  · emit(blocks, delivery="send")：当前 chat 直发（流式 / 进度）

注：跨端点的「主动发送」**不走这里**，而是 agent 显式调用 `send_message` 工具。
两者最终都经同一个出口 Router.submit(outbound)，内容形式同为 blocks。
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent_core.channel.route import RouteEnvelope, normalize_blocks


class TurnResponder:
    def __init__(self, *, router, platform: str, chat_id: str, message_id: str = "", namespace: str = "default") -> None:
        self.router = router
        self.platform = platform
        self.chat_id = chat_id
        self.message_id = message_id
        self.namespace = namespace

    def _base_envelope(self) -> RouteEnvelope:
        addr = f"{self.platform}:{self.chat_id}"
        return RouteEnvelope(
            direction="outbound",
            kind="text",
            delivery="send",
            to=[addr],
            target_addr=addr,
            source_addr=addr,
            agent_key=self.namespace,
        )

    def emit(self, blocks: List[Dict[str, Any]], *, delivery: str = "reply", targets: Any = None) -> Dict[str, Any]:
        """统一发送原语：有序 blocks → 唯一出口。

        · delivery="reply"（默认）：回原消息（reply_to = message_id）
        · delivery="send"：直接发到会话
        · targets 空 = 回来源会话；否则发到指定地址[]
        返回 Router 的投递报告。
        """
        blks = normalize_blocks(blocks)
        if not blks:
            return {"ok": False, "sent": [], "skipped": [], "failed": [], "reasons": {"empty_blocks": 1}}
        env = self._base_envelope()
        env.blocks = blks
        first = str(blks[0].get("type") or "")
        env.kind = first if first in ("image", "file") else "text"
        if first == "image":
            env.images = [
                str(x) for b in blks if b.get("type") == "image" for x in (b.get("images") or [])
            ][:10]
        env.delivery = delivery
        if delivery == "reply" and self.message_id:
            env.reply_to = self.message_id
        if targets:
            items = list(targets) if isinstance(targets, (list, tuple, set)) else [str(targets)]
            items = [str(x) for x in items if str(x)]
            if items:
                env.to = items
                env.target_addr = items[0]
        return self.router.submit(env)


__all__ = ["TurnResponder"]
