from __future__ import annotations

import logging
from typing import Any, Dict

from agent_core.tool.models import ToolResult

logger = logging.getLogger("agent_core")

# 持有 mention_name_map 的引用，handler 通过闭包访问最新数据
_mention_map: Dict[str, str] = {}


def _lookup_user_handler(args: Dict[str, Any], _context: Dict[str, Any]) -> ToolResult:
    from agent_core.channel.feishu.api import get_user_info

    user = (args.get("user") or "").strip()
    if not user:
        return ToolResult(ok=False, error="参数 user 不能为空")

    open_id = _mention_map.get(user) or user

    info = get_user_info(open_id)
    if not info:
        candidates = [k for k, v in _mention_map.items() if user.lower() in k.lower()]
        if candidates:
            info = get_user_info(_mention_map[candidates[0]])
    if not info:
        return ToolResult(ok=False, error=f"未找到用户: {user}，可尝试使用完整的 open_id 查询")

    lines = [f"姓名: {info['name']}"]
    if info.get("en_name"):
        lines.append(f"英文名: {info['en_name']}")
    if info.get("job_title"):
        lines.append(f"职位: {info['job_title']}")
    if info.get("email"):
        lines.append(f"邮箱: {info['email']}")
    if info.get("employee_no"):
        lines.append(f"工号: {info['employee_no']}")
    if info.get("department_ids"):
        lines.append(f"部门ID: {', '.join(info['department_ids'])}")
    return ToolResult(ok=True, data="\n".join(lines))


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


def register_feishu_tools(agent, mention_name_map: Dict[str, str]) -> None:
    """把飞书特有工具注册到 agent 的 tool_registry。

    Args:
        agent: Agent 实例
        mention_name_map: name -> open_id 字典引用（外部更新后自动生效）
    """
    global _mention_map
    _mention_map = mention_name_map

    agent.tool_registry.register(
        "lookup_user",
        _lookup_user_handler,
        description="查询飞书用户的详细信息（姓名、职位、邮箱、部门等）。当你需要了解当前对话用户或群成员的身份背景时调用。",
        parameters={
            "type": "object",
            "properties": {
                "user": {
                    "type": "string",
                    "description": "用户名（如 '张三'）或 open_id（如 'ou_xxx'）",
                }
            },
            "required": ["user"],
        },
    )

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
