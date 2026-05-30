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



def reply_to_message(message_id: str, text: str) -> bool:
    """回复飞书指定消息（使用 markdown 卡片格式）。

    POST /im/v1/messages/{message_id}/reply
    用户在界面上能看到回复关联到原始消息。
    """
    if not message_id or not text:
        return False
    content = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "markdown", "content": text},
        ],
    }
    body = {
        "msg_type": "interactive",
        "content": json.dumps(content, ensure_ascii=False),
    }
    data = _feishu_request("POST", f"/im/v1/messages/{message_id}/reply", body=body)
    if data and data.get("code") == 0:
        return True
    # fallback: 卡片失败则发纯文本
    logger.warning(f"[Feishu] 卡片回复失败, 降级为纯文本: {data}")
    return reply_text_to_message(message_id, text)


def reply_text_to_message(message_id: str, text: str) -> bool:
    """回复飞书指定消息（纯文本格式）。"""
    if not message_id or not text:
        return False
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    body = {
        "msg_type": "text",
        "content": CONTENT_TEMPLATE_TEXT.format(text=escaped),
    }
    data = _feishu_request("POST", f"/im/v1/messages/{message_id}/reply", body=body)
    if data and data.get("code") == 0:
        return True
    logger.error(f"[Feishu] 回复文本失败: {data}")
    return False
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


def add_reaction_to_message(message_id: str, emoji_type: str = "SKULL") -> str:
    """给消息添加 Reaction 表情（默认 SKULL），返回 reaction_id 供后续删除用。

    POST /im/v1/messages/{message_id}/reactions
    """
    if not message_id:
        return ""
    body = {
        "reaction_type": {"emoji_type": emoji_type},
    }
    data = _feishu_request("POST", f"/im/v1/messages/{message_id}/reactions", body=body)
    if data and data.get("code") == 0:
        rid = (data.get("data") or {}).get("reaction_id", "")
        logger.info(f"[Feishu] 已对消息 {message_id[:12]} 添加 reaction {emoji_type} id={rid[:16]}")
        return rid
    # 99991671 = already reacted, 不算失败
    if data and data.get("code") in (99991671,):
        return "already_reacted"
    logger.warning(f"[Feishu] 添加 reaction 失败 msg={message_id[:12]}: {data}")
    return ""


def delete_reaction_to_message(message_id: str, reaction_id: str) -> bool:
    """按 reaction_id 删除消息上的 Reaction 表情。

    调用方必须传入 add_reaction_to_message 返回的 reaction_id：
        rid = add_reaction_to_message(message_id)
        delete_reaction_to_message(message_id, rid)

    DELETE /im/v1/messages/{message_id}/reactions/{reaction_id}
    """
    if not message_id or not reaction_id:
        return False
    token = get_tenant_access_token()
    if not token:
        return False
    import requests
    del_url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.delete(del_url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            logger.info(f"[Feishu] 已删除消息 {message_id[:12]} 的 reaction {reaction_id[:16]}")
            return True
        logger.warning(f"[Feishu] 删除 reaction 失败 msg={message_id[:12]} rid={reaction_id[:16]}: {data}")
        return False
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


def extract_file_info_from_event_message(message: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """从飞书事件消息中提取文件信息 (file_key, file_name)。

    文件消息的 message_type='file', content 格式:
        {"file_key": "xxx", "file_name": "xxx.md"}

    返回 {"file_key": "...", "file_name": "..."} 或 None
    """
    content_str = (message.get("content") or "").strip()
    if not content_str:
        return None
    try:
        content = json.loads(content_str)
        file_key = (content.get("file_key") or "").strip()
        file_name = (content.get("file_name") or "").strip()
        if file_key:
            return {"file_key": file_key, "file_name": file_name or "unknown_file"}
        return None
    except (json.JSONDecodeError, ValueError):
        return None
