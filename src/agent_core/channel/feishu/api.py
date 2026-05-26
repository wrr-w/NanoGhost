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
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("agent_core")


class FeishuTokenManager:
    """飞书 tenant_access_token 多实例缓存管理器。"""

    def __init__(self):
        self._token: Optional[str] = None
        self._expire_at: float = 0.0
        self._lock = threading.Lock()

    def get(self, app_id: str, app_secret: str, force_refresh: bool = False) -> Optional[str]:
        if not force_refresh:
            with self._lock:
                if self._token and self._expire_at - 60 > time.time():
                    return self._token

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            resp = requests.post(url, json={"app_id": app_id, "app_secret": app_secret}, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                with self._lock:
                    self._token = data["tenant_access_token"]
                    self._expire_at = time.time() + float(data.get("expire", 7200))
                return self._token
            logger.error(f"[Feishu] token 换取失败: {data}")
            return None
        except Exception as e:
            logger.error(f"[Feishu] token 请求异常: {e}")
            return None

    def clear(self) -> None:
        with self._lock:
            self._token = None
            self._expire_at = 0.0


def _now() -> float:
    return time.time()


def _get_app_credentials() -> Tuple[str, str]:
    app_id = (os.getenv("FEISHU_APP_ID") or "").strip()
    app_secret = (os.getenv("FEISHU_APP_SECRET") or "").strip()
    return app_id, app_secret


def get_tenant_access_token(force_refresh: bool = False) -> Optional[str]:
    return _shared_token_manager.get(*_get_app_credentials(), force_refresh=force_refresh)


_shared_token_manager = FeishuTokenManager()


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


# ---- 消息反应（Reaction） ----


def add_reaction_to_message(message_id: str, emoji_type: str = "SKULL") -> bool:
    """给消息添加 Reaction 表情（默认 SKULL，完整列表见 lark-im reactions skill）。

    POST /im/v1/messages/{message_id}/reactions
    """
    if not message_id:
        return False
    body = {
        "reaction_type": {"emoji_type": emoji_type},
    }
    data = _feishu_request("POST", f"/im/v1/messages/{message_id}/reactions", body=body)
    if data and data.get("code") == 0:
        logger.info(f"[Feishu] 已对消息 {message_id[:12]} 添加 reaction {emoji_type}")
        return True
    # 99991671 = already reacted, 不算失败
    if data and data.get("code") in (99991671,):
        return True
    logger.warning(f"[Feishu] 添加 reaction 失败 msg={message_id[:12]}: {data}")
    return False


def delete_reaction_to_message(message_id: str, reaction_id: str = "") -> bool:
    """删除消息上的 Reaction 表情。如果不传 reaction_id，会尝试删除第一个 reaction。
    实际使用中通常直接传空字符串，飞书 API 会删除当前用户添加的所有指定类型 reaction。

    DELETE /im/v1/messages/{message_id}/reactions/{reaction_id}
    """
    if not message_id:
        return False
    # 先列出 reaction 找到 ID
    token = get_tenant_access_token()
    if not token:
        return False
    import requests
    list_url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions?page_size=50"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(list_url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("code") != 0:
            logger.warning(f"[Feishu] 列出 reaction 失败 msg={message_id[:12]}: {data}")
            # 尝试直接删除默认 reaction_id
            if reaction_id:
                del_url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}"
                del_resp = requests.delete(del_url, headers=headers, timeout=10)
                del_data = del_resp.json()
                return del_data.get("code") == 0
            return False
        items = data.get("data", {}).get("items", [])
        for item in items:
            rid = item.get("reaction_id") or item.get("id") or ""
            if rid:
                del_url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions/{rid}"
                del_resp = requests.delete(del_url, headers=headers, timeout=10)
                if del_resp.json().get("code") == 0:
                    logger.info(f"[Feishu] 已删除消息 {message_id[:12]} 的 reaction {rid[:12]}")
                    return True
        logger.info(f"[Feishu] 消息 {message_id[:12]} 没有可删除的 reaction")
        return True
    except Exception as e:
        logger.warning(f"[Feishu] 删除 reaction 异常 msg={message_id[:12]}: {e}")
        return False


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
