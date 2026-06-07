# -*- coding: utf-8 -*-
"""
飞书传输层：WebSocket 长连接管理。

仅负责：
1. 启动/维护飞书 WebSocket SDK 连接
2. 接收事件并回调外部处理器
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

logger = logging.getLogger("agent_core")


class FeishuSDK:
    """飞书 WebSocket SDK 长连接管理器。"""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._on_message = lambda _: None

    def set_message_handler(self, handler):
        """注入消息处理器。"""
        self._on_message = handler

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="feishu-ws-sdk",
        )
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        import lark_oapi as lark
        import lark_oapi.ws.client as ws_client

        # 日志配置
        lark_logger = logging.getLogger("Lark")
        lark_logger.handlers.clear()
        lark_logger.propagate = True

        _inst = os.environ.get("INSTANCE_DIR", "")
        if _inst:
            _log_dir = os.path.join(_inst, "runtime")
            os.makedirs(_log_dir, exist_ok=True)
            _log_path = os.path.join(_log_dir, "feishu.log")
            if not any(
                isinstance(h, logging.FileHandler)
                and h.baseFilename == os.path.abspath(_log_path)
                for h in logging.getLogger().handlers
            ):
                _fh = logging.FileHandler(_log_path, encoding="utf-8")
                _fh.setFormatter(logging.Formatter(
                    "%(asctime)s | %(levelname)-5s | feeld | %(message)s"
                ))
                logging.getLogger().addHandler(_fh)

        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        ws_client.loop = new_loop

        _noop = lambda _: None
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_customized_event("im.message.reaction.created_v1", _noop)
            .register_p2_customized_event("im.message.reaction.deleted_v1", _noop)
            .register_p2_customized_event("im.message.message_read_v1", _noop)
            .register_p2_customized_event("im.chat.access_event.bot_p2p_chat_entered_v1", _noop)
            .build()
        )

        client = lark.ws.Client(
            os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"],
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )
        client.on_reconnected = lambda: logger.info("[Feishu SDK] 已重连")
        logger.info("[Feishu SDK] 正在连接飞书 WebSocket ...")
        client.start()
