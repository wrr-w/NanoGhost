# -*- coding: utf-8 -*-
"""当前会话输出边界。

把 Presenter 中直接依赖 `ChannelIO` 的发送动作收敛成一个薄包装：
  · reply_current()：当前消息回复
  · send_current()：当前 chat 直发
  · send_images()：当前 chat 发图
  · notify()：跨端点主动发送（经 Router）
"""

from __future__ import annotations

from typing import Iterable

from agent_core.channel.route import RouteEnvelope, make_image_block, make_text_block

class TurnResponder:
    def __init__(self, *, router, platform: str, chat_id: str, message_id: str = "", namespace: str = "default") -> None:
        self.router = router
        self.platform = platform
        self.chat_id = chat_id
        self.message_id = message_id
        self.namespace = namespace

    def _base_envelope(self, text: str = "") -> RouteEnvelope:
        return RouteEnvelope(
            direction="outbound",
            kind="text",
            delivery="send",
            to=[f"{self.platform}:{self.chat_id}"],
            target_addr=f"{self.platform}:{self.chat_id}",
            text=text,
            blocks=([make_text_block(text)] if text else []),
            source_addr=f"{self.platform}:{self.chat_id}",
            agent_key=self.namespace,
        )

    def reply_current(self, text: str) -> bool:
        env = self._base_envelope(text)
        env.delivery = "reply"
        env.reply_to = self.message_id or None
        return bool(self.router.submit(env).get("ok"))

    def send_current(self, text: str) -> bool:
        return bool(self.router.submit(self._base_envelope(text)).get("ok"))

    def send_images(self, images: Iterable[str]) -> None:
        items = list(images)
        if items:
            env = self._base_envelope()
            env.kind = "image"
            env.images = items[:10]
            env.blocks = [make_image_block(items[:10])]
            self.router.submit(env)

    def notify(self, to, text: str, *, namespace: str | None = None):
        targets = list(to) if isinstance(to, (list, tuple, set)) else [str(to)]
        env = RouteEnvelope(
            direction="outbound",
            kind="text",
            delivery="send",
            to=targets,
            target_addr=(targets[0] if targets else ""),
            text=text,
            blocks=([make_text_block(text)] if text else []),
            source_addr=f"{self.platform}:{self.chat_id}",
            agent_key=namespace or self.namespace,
        )
        return self.router.submit(env)


__all__ = ["TurnResponder"]
