"""
飞书（Feishu/Lark）REST API 封装：
- tenant_access_token 获取（带缓存）
- 发送文本/图片消息
- 下载消息资源
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("agent_core")

_TOKEN_CACHE: Dict[str, Any] = {"token": None, "expire_at": 0.0}


def _now() -> float:
    return time.time()


def _get_app_credentials() -> Tuple[str, str]:
    app_id = (os.getenv("FEISHU_APP_ID") or "").strip()
    app_secret = (os.getenv("FEISHU_APP_SECRET") or "").strip()
    return app_id, app_secret


def get_tenant_access_token(force_refresh: bool = False) -> Optional[str]:
    if not force_refresh:
        token = _TOKEN_CACHE.get("token")
        expire_at = float(_TOKEN_CACHE.get("expire_at") or 0)
        if token and expire_at - 60 > _now():
            return token

    app_id, app_secret = _get_app_credentials()
    if not app_id or not app_secret:
        logger.warning("[Feishu] FEISHU_APP_ID/FEISHU_APP_SECRET 未配置")
        return None

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    try:
        resp = requests.post(url, json={"app_id": app_id, "app_secret": app_secret}, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            _TOKEN_CACHE["token"] = data["tenant_access_token"]
            _TOKEN_CACHE["expire_at"] = _now() + float(data.get("expire", 7200))
            return data["tenant_access_token"]
        logger.error(f"[Feishu] token 换取失败: {data}")
        return None
    except Exception as e:
        logger.error(f"[Feishu] token 请求异常: {e}")
        return None


def _feishu_request(method: str, path: str, body: Optional[Dict] = None) -> Optional[Dict]:
    """带 token 的飞书 OpenAPI 请求（自动重试 token 过期）。"""
    token = get_tenant_access_token()
    if not token:
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    url = f"https://open.feishu.cn/open-apis{path}"
    try:
        resp = requests.request(method, url, json=body, headers=headers, timeout=30)
        data = resp.json()
        code = data.get("code", -1)
        if code == 99991663 or code == 99991664:
            token = get_tenant_access_token(force_refresh=True)
            if token:
                headers["Authorization"] = f"Bearer {token}"
                resp = requests.request(method, url, json=body, headers=headers, timeout=30)
                data = resp.json()
        return data
    except Exception as e:
        logger.error(f"[Feishu] request error {path}: {e}")
        return None


# ---- 消息发送 ----

CONTENT_TEMPLATE_TEXT = '{{"text":"{text}"}}'


def send_text_message_to_chat(chat_id: str, text: str) -> bool:
    """发送纯文本消息到飞书会话。"""
    if not chat_id or not text:
        return False
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    body = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": CONTENT_TEMPLATE_TEXT.format(text=escaped),
    }
    data = _feishu_request("POST", "/im/v1/messages?receive_id_type=chat_id", body=body)
    if data and data.get("code") == 0:
        return True
    logger.error(f"[Feishu] 发送文本消息失败: {data}")
    return False


def send_markdown_message_to_chat(chat_id: str, text: str) -> bool:
    """发送 markdown 格式消息到飞书会话（使用 interactive 卡片）。

    支持 **粗体**、- 列表、`行内代码`、标题、换行等标准 markdown 语法。
    """
    if not chat_id or not text:
        return False
    content = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "markdown", "content": text},
        ],
    }
    body = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(content, ensure_ascii=False),
    }
    data = _feishu_request("POST", "/im/v1/messages?receive_id_type=chat_id", body=body)
    if data and data.get("code") == 0:
        return True
    # fallback: 卡片失败则发纯文本
    logger.warning(f"[Feishu] 卡片消息失败, 降级为纯文本: {data}")
    return send_text_message_to_chat(chat_id, text)


def send_images_base64_to_chat(chat_id: str, images_base64: List[str]) -> Dict[str, Any]:
    """发送多张图片到飞书。返回 {ok, sent, failed, errors}。"""
    result: Dict[str, Any] = {"ok": True, "sent": 0, "failed": 0, "errors": []}
    for b64 in images_base64 or []:
        if not b64 or not b64.startswith("data:image/"):
            result["failed"] += 1
            continue
        try:
            mime_part = b64.split(";")[0]
            image_type = mime_part.replace("data:image/", "")
            b64_data = b64.split(",")[1] if "," in b64 else b64
            body = {
                "receive_id": chat_id,
                "msg_type": "image",
                "content": json.dumps({"image_type": image_type, "image": b64_data}),
            }
            data = _feishu_request("POST", "/im/v1/messages?receive_id_type=chat_id", body=body)
            if data and data.get("code") == 0:
                result["sent"] += 1
            else:
                result["failed"] += 1
                result["errors"].append(str(data))
        except Exception as e:
            result["failed"] += 1
            result["errors"].append(str(e))
    return result


# ---- 资源下载 ----

def download_message_resource(
    message_id: str, file_key: str, resource_type: str = "image",
) -> Optional[Tuple[bytes, str]]:
    """下载消息中的资源（图片、文件）。返回 (bytes, content_type)。"""
    token = get_tenant_access_token()
    if not token:
        return None
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type={resource_type}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            ct = resp.headers.get("Content-Type") or resp.headers.get("content-type") or "image/png"
            return (resp.content, ct)
        logger.error(f"[Feishu] 下载资源失败: {resp.status_code} {resp.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"[Feishu] 下载资源异常: {e}")
        return None


# ---- 消息解析 ----

def extract_text_from_event_message(message: Dict[str, Any]) -> str:
    """从飞书事件消息中提取纯文本内容。"""
    content_str = (message.get("content") or message.get("text") or "").strip()
    if not content_str:
        return ""
    try:
        content = json.loads(content_str)
        return (content.get("text") or "").strip()
    except (json.JSONDecodeError, ValueError):
        return content_str


def extract_image_keys_from_event_message(message: Dict[str, Any]) -> List[str]:
    """从飞书事件消息中提取 image_key 列表。"""
    content_str = (message.get("content") or "").strip()
    if not content_str:
        return []
    try:
        content = json.loads(content_str)
        keys = content.get("image_key", content.get("file_key", ""))
        return [keys] if keys else []
    except (json.JSONDecodeError, ValueError):
        return []
