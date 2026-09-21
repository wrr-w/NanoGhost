from __future__ import annotations

import logging
from typing import Any, Dict

from agent_core.tool.models import ToolResult

logger = logging.getLogger("agent_core")

def _recall_image_handler(args: Dict[str, Any], context: Dict[str, Any]) -> ToolResult:
    """查看之前存储在 session 中的图片。"""
    image_id = (args.get("image_id") or "").strip()
    if not image_id:
        return ToolResult(ok=False, error="参数 image_id 不能为空")

    db = context.get("db")
    if not db:
        return ToolResult(ok=False, error="db 不可用")

    imgs = db.get_agent_images_batch([image_id])
    if not imgs:
        return ToolResult(ok=False, error=f"未找到图片: {image_id}")
    img = imgs[0]
    b64 = img.get("base64") if isinstance(img, dict) else ""
    if not b64:
        return ToolResult(ok=False, error=f"图片数据为空: {image_id}")
    return ToolResult(ok=True, data=f"图片 {image_id}:", images={image_id: b64})


def register_feishu_tools(agent, mention_name_map: Dict[str, str] = None) -> None:
    """把飞书特有工具注册到 agent 的 tool_registry。

    Args:
        agent: Agent 实例
        mention_name_map: 保留参数（兼容调用方）；用户身份查询已统一交 lark-cli
            （`lark-cli contact +get-user` / `+search-user`），不再自建 lookup_user。
    """
    agent.tool_registry.register(
        "recall_image",
        _recall_image_handler,
        description="查看当前 session 中存储的图片。用户之前发过的图片存储在 session 中，用此工具可以回顾。通过 image_id 指定图片（如 img_0, img_1）。",
        parameters={
            "type": "object",
            "properties": {
                "image_id": {
                    "type": "string",
                    "description": "图片 ID（如 img_0、img_1）",
                }
            },
            "required": ["image_id"],
        },
    )
