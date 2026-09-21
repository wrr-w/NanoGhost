# -*- coding: utf-8 -*-
"""兼容层：`FeishuWSClient` 已并入 `FeishuChannel`（乙b 组合重构）。

原「编排器 FeishuWSClient」的职责已拆分：
  · 接收管道 → `transport.FeishuTransport`
  · 归一/编排 → `channel.FeishuChannel`

保留本别名，避免外部旧引用断裂。
"""

from __future__ import annotations

from .channel import FeishuChannel

# 旧名 → 新门面（同一个类）
FeishuWSClient = FeishuChannel

__all__ = ["FeishuWSClient", "FeishuChannel"]
