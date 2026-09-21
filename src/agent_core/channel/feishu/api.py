from __future__ import annotations

"""
飞书（Feishu/Lark）REST API 封装：
- tenant_access_token 获取（带缓存）
- 发送文本/图片消息
- 下载消息资源
"""

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

# 用户名缓存: open_id -> name
_user_name_cache: Dict[str, str] = {}


def get_user_name(open_id: str) -> Optional[str]:
    """按 open_id 获取飞书用户显示名。带内存缓存。"""
    if not open_id:
        return None
    if open_id in _user_name_cache:
        return _user_name_cache[open_id]
    path = f"/contact/v3/users/{open_id}?user_id_type=open_id"
    data = _feishu_request("GET", path)
    name = None
    if data and data.get("code") == 0:
        user = data.get("data", {}).get("user", {})
        if user:
            name = (user.get("name") or user.get("nickname") or "").strip() or None
        if not name:
            logger.warning(f"[Feishu] get_user_name({open_id}) 返回空 name, user keys={list(user.keys()) if user else 'None'}")
    else:
        code = data.get("code") if data else "N/A"
        msg = data.get("msg") if data else "N/A"
        logger.warning(f"[Feishu] get_user_name({open_id}) 失败 code={code} msg={msg}")
    if name:
        _user_name_cache[open_id] = name
    return name


class FeishuTokenManager:
    """飞书 tenant_access_token 多实例缓存管理器。按 (app_id, app_secret) 键隔离。"""

    def __init__(self):
        self._tokens: Dict[Tuple[str, str], Tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _key(self, app_id: str, app_secret: str) -> Tuple[str, str]:
        return (app_id, app_secret)

    def get(self, app_id: str, app_secret: str, force_refresh: bool = False) -> Optional[str]:
        k = self._key(app_id, app_secret)
        if not force_refresh:
            with self._lock:
                entry = self._tokens.get(k)
                if entry and entry[1] - 60 > time.time():
                    return entry[0]

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            resp = requests.post(url, json={"app_id": app_id, "app_secret": app_secret}, timeout=10)
            data = resp.json()
            if data.get("code") == 0:
                token = data["tenant_access_token"]
                expire_at = time.time() + float(data.get("expire", 7200))
                with self._lock:
                    self._tokens[k] = (token, expire_at)
                return token
            logger.error(f"[Feishu] token 换取失败: {data}")
            return None
        except Exception as e:
            logger.error(f"[Feishu] token 请求异常: {e}")
            return None

    def clear(self, app_id: str = "", app_secret: str = "") -> None:
        with self._lock:
            if app_id and app_secret:
                self._tokens.pop(self._key(app_id, app_secret), None)
            else:
                self._tokens.clear()


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


# ---- 文件上传 / 发送 ----

#: 飞书「文件」消息单文件上限 30MB
MAX_FILE_BYTES = 30 * 1024 * 1024

_FILE_TYPE_MAP = {
    "pdf": "pdf",
    "doc": "doc", "docx": "doc",
    "xls": "xls", "xlsx": "xls", "csv": "xls",
    "ppt": "ppt", "pptx": "ppt",
    "mp4": "mp4",
    "opus": "opus",
}


def guess_file_type(file_name: str) -> str:
    """按扩展名推飞书 file_type；未知一律 stream。"""
    ext = os.path.splitext(str(file_name or ""))[1].lower().lstrip(".")
    return _FILE_TYPE_MAP.get(ext, "stream")


def upload_file(path: str, *, file_name: str = "", file_type: str = "") -> Optional[str]:
    """上传本机文件到飞书，返回 file_key；失败返回 None。

    POST /im/v1/files  (multipart/form-data: file_type, file_name, file)
    """
    name = file_name or os.path.basename(str(path or "")) or "file"
    ftype = file_type or guess_file_type(name)
    token = get_tenant_access_token()
    if not token:
        return None
    url = "https://open.feishu.cn/open-apis/im/v1/files"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with open(path, "rb") as fh:
            files = {"file": (name, fh, "application/octet-stream")}
            data = {"file_type": ftype, "file_name": name}
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=120)
        js = resp.json()
        if js.get("code") == 0:
            return (js.get("data") or {}).get("file_key")
        logger.error(f"[Feishu] 上传文件失败 name={name} type={ftype}: {js}")
        return None
    except Exception as e:  # noqa: BLE001
        logger.error(f"[Feishu] 上传文件异常 name={name}: {e}")
        return None


def send_file_to_chat(chat_id: str, file_key: str, file_name: str = "") -> bool:
    """发送已上传的文件（msg_type=file）到会话。"""
    if not chat_id or not file_key:
        return False
    body = {
        "receive_id": chat_id,
        "msg_type": "file",
        "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
    }
    data = _feishu_request("POST", "/im/v1/messages?receive_id_type=chat_id", body=body)
    if data and data.get("code") == 0:
        return True
    logger.error(f"[Feishu] 发送文件消息失败 name={file_name} file_key={file_key}: {data}")
    return False


def send_local_files_to_chat(chat_id: str, files: List[Any]) -> Dict[str, Any]:
    """把一批本机文件上传并发送到会话。返回 {ok, sent, failed, errors}。

    files 元素可为路径字符串，或 {"path": ..., "name": ...}。
    只处理本机真实文件（存在 + ≤30MB）；**允许任意目录**。
    """
    result: Dict[str, Any] = {"ok": True, "sent": 0, "failed": 0, "errors": []}
    for item in list(files or []):
        if isinstance(item, dict):
            path = str(item.get("path") or "").strip()
            name = str(item.get("name") or "").strip()
        else:
            path = str(item or "").strip()
            name = ""
        if not path or not os.path.isfile(path):
            result["failed"] += 1
            result["errors"].append(f"not_found:{path}")
            continue
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            result["failed"] += 1
            result["errors"].append(f"too_large:{os.path.basename(path)}({size}B)")
            continue
        if not name:
            name = os.path.basename(path)
        fkey = upload_file(path, file_name=name)
        if not fkey:
            result["failed"] += 1
            result["errors"].append(f"upload_failed:{name}")
            continue
        if send_file_to_chat(chat_id, fkey, name):
            result["sent"] += 1
        else:
            result["failed"] += 1
            result["errors"].append(f"send_failed:{name}")
    result["ok"] = result["sent"] > 0
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
# ---- 消息获取 ----

def get_message_by_id(message_id: str) -> Optional[Dict[str, Any]]:
    """获取飞书消息内容（用于查看引用/回复的原始消息）。

    GET /open-apis/im/v1/messages/{message_id}
    返回 {"content": "...", "msg_type": "...", "sender": {...}} 或 None
    """
    if not message_id:
        return None
    data = _feishu_request("GET", f"/im/v1/messages/{message_id}")
    if data and data.get("code") == 0:
        msg_item = data.get("data", {}).get("items", [None])[0]
        if msg_item:
            body = msg_item.get("body", {}) or {}
            sender = msg_item.get("sender", {}) or {}
            # msg_type 可能在 item 层也在 body 层，都试
            msg_type = msg_item.get("msg_type") or body.get("msg_type") or body.get("content_type") or ""
            return {
                "content": body.get("content", ""),
                "msg_type": msg_type,
                "sender": sender,
            }
        else:
            logger.warning(f"[Feishu] get_message_by_id({message_id}) 返回空 items")
    else:
        code = data.get("code") if data else "N/A"
        msg = data.get("msg") if data else "N/A"
        logger.warning(f"[Feishu] get_message_by_id({message_id}) 失败 code={code} msg={msg}")
    return None


def parse_message_content(content_str: str, msg_type: str) -> str:
    """根据消息类型解析 content JSON，提取可读文本。"""
    if not content_str:
        return ""
    try:
        content = json.loads(content_str)
        # content 可能是 JSON 数组（卡片消息的 elements 数组）
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("tag") == "text":
                        texts.append(item.get("text", ""))
                    elif item.get("tag") == "markdown":
                        texts.append(item.get("content", ""))
                    elif item.get("tag") == "a":
                        texts.append(item.get("text", "") or item.get("href", ""))
            return "".join(texts).strip()[:500]
        if msg_type == "text":
            return (content.get("text") or "").strip()
        elif msg_type == "post":
            post = content.get("zh_cn") or content.get("en_us") or {}
            paragraphs = post.get("content") or []
            texts = []
            for para in paragraphs:
                for block in para:
                    if block.get("tag") == "text":
                        texts.append(block.get("text", ""))
                    elif block.get("tag") == "a":
                        texts.append(block.get("text", "") or block.get("href", ""))
            return "".join(texts)
        elif msg_type == "interactive":
            elements = content.get("elements") or []
            texts = []
            # 卡片标题
            title = content.get("header", {}).get("title", {})
            if isinstance(title, dict):
                for block in (title.get("elements") or []):
                    if block.get("tag") == "text":
                        texts.append(block.get("text", ""))
            for elem in elements:
                if elem.get("tag") == "markdown":
                    texts.append(elem.get("content", ""))
                elif elem.get("tag") == "text":
                    texts.append(elem.get("text", ""))
                elif elem.get("tag") == "column_set":
                    # 卡片中的列布局，递归提取子元素
                    for col in (elem.get("flex_layout", []) + elem.get("columns", [])):
                        for child in (col.get("elements", []) if isinstance(col, dict) else []):
                            if child.get("tag") == "markdown":
                                texts.append(child.get("content", ""))
                            elif child.get("tag") == "text":
                                texts.append(child.get("text", ""))
            return "".join(texts)[:500]
        else:
            raw = str(content)
            return raw[:200] + ("..." if len(raw) > 200 else "")
    except (json.JSONDecodeError, ValueError):
        return content_str[:200]

def get_chat_name(chat_id: str) -> Optional[str]:
    "获取飞书群聊名称。"
    if not chat_id:
        return None
    data = _feishu_request("GET", f"/im/v1/chats/{chat_id}")
    if data and data.get("code") == 0:
        info = data.get("data", {})
        return info.get("name") or None
    return None


def get_thread_messages(thread_id: str, exclude_message_id: str = "") -> List[Dict[str, Any]]:
    """获取话题（thread）内的消息列表。返回 [{sender_name, content, message_id}, ...]。"""
    if not thread_id:
        return []
    result: List[Dict[str, Any]] = []
    page_token = ""
    pages = 0
    while True:
        pages += 1
        if pages > 10:
            break
        params = f"container_id_type=thread&container_id={thread_id}&page_size=50&sort_type=ByCreateTimeAsc"
        if page_token:
            params += f"&page_token={page_token}"
        data = _feishu_request("GET", f"/im/v1/messages?{params}")
        if not data or data.get("code") != 0:
            break
        items = data.get("data", {}).get("items", []) or []
        for it in items:
            mid = (it.get("message_id") or "").strip()
            if exclude_message_id and mid == exclude_message_id:
                continue
            body = it.get("body", {}) or {}
            sender = it.get("sender", {}) or {}
            msg_type = it.get("msg_type") or body.get("msg_type") or body.get("content_type") or ""
            content_str = body.get("content", "") or ""
            parsed = parse_message_content(content_str, msg_type)
            if not parsed:
                continue
            sender_id = (sender.get("id") or "").strip()
            sender_name = get_user_name(sender_id) or sender_id[:12] or "用户"
            result.append({"message_id": mid, "sender_name": sender_name, "content": parsed[:500]})
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token", "")
        if not page_token:
            break
    return result


def get_bot_info() -> Optional[Dict[str, str]]:
    """获取当前 bot 自身信息。返回 {"open_id": "...", "name": "..."} 或 None。"""
    data = _feishu_request("GET", "/bot/v3/info")
    if data and data.get("code") == 0:
        bot = data.get("bot", {})
        if bot:
            return {
                "open_id": bot.get("open_id", ""),
                "name": bot.get("app_name", ""),
            }
    # 打详细日志以便诊断
    code = data.get("code") if data else "N/A"
    msg = data.get("msg") if data else "N/A"
    logger.warning(f"[Feishu] get_bot_info FAILED: app_id={os.getenv('FEISHU_APP_ID','?')[:20]}... code={code} msg={msg}")
    return None
