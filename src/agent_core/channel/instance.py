# -*- coding: utf-8 -*-
"""
通用实例层：Bot 身份、Prompts、Memory。

所有渠道共用，不依赖具体 SDK。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("agent_core")


class BotInstance:
    """Bot 实例级别的基础配置和状态。"""

    def __init__(self, sys_prompt: str = "", bot_name: str = "", bot_id: str = ""):
        self._base_sys_prompt = sys_prompt
        self.bot_name = bot_name
        self.bot_id = bot_id
        self._feedback_level = 2
        self.history_max_messages = 200
        self.history_max_tokens = 200_000

    def set_feedback_level(self, level: int):
        self._feedback_level = max(1, min(4, level))

    def get_feedback_level(self) -> int:
        return self._feedback_level

    def get_base_sys_prompt(self) -> str:
        return self._base_sys_prompt

    def refresh_memory(self, instance_dir: str = ""):
        """读取 memory.md 并注入 sys_prompt。"""
        if not instance_dir:
            instance_dir = os.environ.get("INSTANCE_DIR", "")
        if not instance_dir:
            return
        memory_path = os.path.join(instance_dir, "memory.md")
        if not os.path.isfile(memory_path):
            return
        try:
            with open(memory_path, encoding="utf-8") as f:
                memory_content = f.read().strip()
            if not memory_content:
                return
            marker = "## 记住的信息"
            if marker in self._base_sys_prompt:
                idx = self._base_sys_prompt.find(marker)
                self._base_sys_prompt = self._base_sys_prompt[:idx].rstrip()
            self._base_sys_prompt += "\n\n## 记住的信息\n\n" + memory_content + "\n\n"
        except Exception:
            pass

    def load_feedback_level(self, instance_dir: str = ""):
        """从实例 config.yaml 加载反馈级别。"""
        if not instance_dir:
            instance_dir = os.environ.get("INSTANCE_DIR", "")
        if not instance_dir:
            return
        cfg_path = os.path.join(instance_dir, "config.yaml")
        try:
            import yaml
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            level = int(cfg.get("feedback_level", 2))
            self.set_feedback_level(level)
            logger.info(f"[BotInstance] feedback_level={level}")
        except Exception:
            pass

    def load_history_limits(self, instance_dir: str = ""):
        """从实例 config.yaml 加载历史消息限制。"""
        if not instance_dir:
            instance_dir = os.environ.get("INSTANCE_DIR", "")
        if not instance_dir:
            return
        cfg_path = os.path.join(instance_dir, "config.yaml")
        try:
            import yaml
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            history_cfg = cfg.get("history")
            if isinstance(history_cfg, dict):
                if "max_messages" in history_cfg:
                    self.history_max_messages = int(history_cfg["max_messages"])
                if "max_tokens" in history_cfg:
                    self.history_max_tokens = int(history_cfg["max_tokens"])
            logger.info(f"[BotInstance] history_limits={self.history_max_messages} msgs / {self.history_max_tokens} tokens")
        except Exception:
            pass
