# -*- coding: utf-8 -*-
"""统一路由信封、消息块与路由策略。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def make_text_block(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": str(text or "")}


def make_markdown_block(text: str) -> Dict[str, Any]:
    return {"type": "markdown", "text": str(text or "")}


def make_image_block(images: List[str]) -> Dict[str, Any]:
    return {"type": "image", "images": [str(x) for x in list(images or []) if str(x)]}


def normalize_blocks(blocks: List[Dict[str, Any]] | None, *, text: str = "", images: List[str] | None = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for block in list(blocks or []):
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").strip().lower()
        if btype in ("text", "markdown"):
            value = str(block.get("text") or "")
            if value.strip():
                out.append({"type": btype, "text": value})
        elif btype == "image":
            items = [str(x) for x in list(block.get("images") or []) if str(x)]
            if items:
                out.append({"type": "image", "images": items[:10]})
    if out:
        return out
    text = str(text or "")
    images = [str(x) for x in list(images or []) if str(x)]
    if text.strip():
        out.append(make_text_block(text))
    if images:
        out.append(make_image_block(images[:10]))
    return out


@dataclass
class RouteEnvelope:
    direction: str
    kind: str
    delivery: str = "reply"
    source_addr: str = ""
    target_addr: str = ""
    to: List[str] = field(default_factory=list)
    payload: Any = None
    text: str = ""
    images: List[str] = field(default_factory=list)
    blocks: List[Dict[str, Any]] = field(default_factory=list)
    reply_to: Optional[str] = None
    reaction: Optional[Dict[str, Any]] = None
    agent_key: str = "default"
    summary: str = ""
    route_key: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


def resolve_reply_policy(*, endpoint, channel_policy: Dict[str, Any]) -> Dict[str, bool]:
    meta = dict(getattr(endpoint, "meta", {}) or {})
    supports_reply = bool(channel_policy.get("supports_reply", False))
    allow_reply = meta["allow_reply"] if "allow_reply" in meta else bool(channel_policy.get("default_allow_reply", False))
    prefer_reply = meta["prefer_reply"] if "prefer_reply" in meta else bool(channel_policy.get("default_prefer_reply", False))
    return {
        "supports_reply": supports_reply,
        "allow_reply": bool(allow_reply),
        "prefer_reply": bool(prefer_reply),
    }


__all__ = [
    "RouteEnvelope",
    "make_text_block",
    "make_markdown_block",
    "make_image_block",
    "normalize_blocks",
    "resolve_reply_policy",
]
