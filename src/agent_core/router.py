# -*- coding: utf-8 -*-
"""出站路由（Router）—— P2。

职责：把「语义目标」落成「具体端点地址」，并按通道投递（fan-out），统一挂上护栏。
**模型自由路由**：模型可指名任意端点（0..N）。

    · resolve(to, ctx) : 语义目标 → 地址列表（不填 = 回来源）
    · deliver(targets) : 逐个投递（走 ChannelRegistry）+ 护栏
    · send(to, text)   : resolve + deliver 一步到位

护栏（自由 ≠ 无约束）：
    · ACL（按 agent 授权）：这个 agent 能不能发给这个端点
    · 防环：hop 计数（转发 hop+1，超上限拒绝）
    · 限速：每端点每窗口发送上限
    · 审计：每次外发都记一笔
    · 默认兜底：没说目标 → 回来源

出站镜像：投递成功后，若目标 != 来源会话，调 mirror 钩子把这条也写进**目标会话**
（实现由使用方注入，见 feishu/ws_client._mirror_outbound）。回来源由本轮回复负责，天然一致。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from agent_core.channel.base import Channel
from agent_core.channel.endpoint import parse_addr
from agent_core.channel.route import RouteEnvelope, normalize_blocks
from agent_core.channel.registry import get_registry
from agent_core.runtime.inbox import InboxEvent, get_hub

logger = logging.getLogger("agent_core")

#: 防环：转发链最大跳数
MAX_HOP = 4


# ══════════════════════════════════════════════════════════
# 护栏：ACL（按 agent 授权）
# ══════════════════════════════════════════════════════════
class Acl:
    """按 agent 的端点可见性。默认全放开；显式 set 后按白名单（支持 '*' 前缀）。"""

    def __init__(self) -> None:
        self._rules: Dict[str, Optional[Iterable[str]]] = {}
        self._lock = threading.Lock()

    def set(self, agent_key: str, allowed: Iterable[str]) -> None:
        with self._lock:
            self._rules[agent_key] = set(allowed)

    def clear(self, agent_key: str) -> None:
        with self._lock:
            self._rules.pop(agent_key, None)

    @staticmethod
    def _match(addr: str, pattern: str) -> bool:
        if pattern == "*":
            return True
        if pattern.endswith("*"):
            return addr.startswith(pattern[:-1])
        return addr == pattern

    def allow(self, agent_key: str, addr: str) -> bool:
        with self._lock:
            if agent_key not in self._rules:
                return True          # 未配置 = 放开（不破坏现状）
            allowed = self._rules[agent_key]
            if allowed is None:
                return True
            return any(self._match(addr, p) for p in allowed)


# ══════════════════════════════════════════════════════════
# 护栏：限速（每端点每窗口）
# ══════════════════════════════════════════════════════════
class RateLimiter:
    def __init__(self, per_window: int = 20, window: float = 60.0) -> None:
        self.per_window = per_window
        self.window = window
        self._hits: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.per_window:
                return False
            q.append(now)
            return True


# ══════════════════════════════════════════════════════════
# 护栏：审计
# ══════════════════════════════════════════════════════════
class Auditor:
    def __init__(self, capacity: int = 200) -> None:
        self._log: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def record(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            self._log.append(rec)

    def recent(self, n: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._log)[-n:]


# ══════════════════════════════════════════════════════════
# Router
# ══════════════════════════════════════════════════════════
class Router:
    def __init__(
        self,
        registry=None,
        *,
        manager=None,
        mirror: Optional[Callable[[str, str], None]] = None,
        hub=None,
    ) -> None:
        self.registry = registry
        self.manager = manager
        self.mirror = mirror
        self.hub = hub or get_hub()
        self.acl = Acl()
        self.rate = RateLimiter()
        self.auditor = Auditor()

    def set_mirror(self, fn: Optional[Callable[[str, str], None]]) -> None:
        self.mirror = fn

    def _registry(self):
        return self.registry or get_registry()

    def _manager(self):
        if self.manager is not None:
            return self.manager
        if self.registry is not None:
            from agent_core.channel.directory import EndpointDirectory
            from agent_core.channel.manager import ChannelManager
            return ChannelManager(registry=self._registry(), directory=EndpointDirectory())
        from agent_core.channel.manager import get_channel_manager
        return get_channel_manager()

    @staticmethod
    def _split_addr(addr: str) -> Tuple[str, str]:
        return parse_addr(addr) if ":" in addr else ("", addr)

    def normalize(self, envelope: RouteEnvelope) -> RouteEnvelope:
        envelope.direction = (envelope.direction or "").strip().lower()
        envelope.kind = (envelope.kind or "event").strip()
        envelope.delivery = (envelope.delivery or "reply").strip().lower() or "reply"
        envelope.source_addr = (envelope.source_addr or "").strip()
        envelope.target_addr = (envelope.target_addr or "").strip()
        envelope.to = [str(x).strip() for x in list(envelope.to or []) if str(x).strip()]
        envelope.text = str(envelope.text or "")
        envelope.images = [str(x) for x in list(envelope.images or []) if str(x)]
        envelope.blocks = normalize_blocks(envelope.blocks, text=envelope.text, images=envelope.images)
        envelope.summary = str(envelope.summary or "")
        envelope.meta = dict(envelope.meta or {})
        return envelope

    @staticmethod
    def _block_preview(blocks: List[Dict[str, Any]]) -> str:
        previews: List[str] = []
        for block in blocks[:3]:
            btype = str(block.get("type") or "")
            if btype in ("text", "markdown"):
                txt = str(block.get("text") or "").strip()
                if txt:
                    previews.append(txt[:40])
            elif btype == "image":
                previews.append(f"[images:{len(list(block.get('images') or []))}]")
        return " | ".join(previews) or "[blocks]"

    def _send_blocks_via_channel(
        self,
        ch,
        *,
        target: str,
        blocks: List[Dict[str, Any]],
        delivery: str,
        reply_to: str | None,
    ) -> bool:
        sender = getattr(ch, "send_blocks", None)
        if callable(sender) and getattr(getattr(sender, "__func__", None), "__qualname__", "") != Channel.send_blocks.__qualname__:
            return bool(sender(target, blocks, delivery=delivery, reply_to=reply_to))

        used_reply = False
        for block in blocks:
            btype = str(block.get("type") or "").strip().lower()
            if btype in ("text", "markdown"):
                text = str(block.get("text") or "")
                if not text.strip():
                    continue
                should_reply = bool(delivery == "reply" and reply_to and not used_reply)
                if should_reply:
                    ok = bool(ch.reply(reply_to or "", text))
                    used_reply = True
                else:
                    ok = bool(ch.send(target, text))
            elif btype == "image":
                sender = getattr(ch, "send_images", None)
                images = [str(x) for x in list(block.get("images") or []) if str(x)]
                if not callable(sender) or not images:
                    return False
                sender(target, images[:10])
                ok = True
            else:
                return False
            if not ok:
                return False
        return True

    def submit(self, envelope: RouteEnvelope, *, hop: int = 0):
        env = self.normalize(envelope)
        if env.direction == "inbound":
            return self.route_inbound(env)
        if env.direction == "outbound":
            return self.route_outbound(env, hop=hop)
        return {"ok": False, "error": f"invalid direction: {env.direction}"}

    def adapt_to_inbox(self, envelope: RouteEnvelope) -> InboxEvent:
        return InboxEvent(
            target=envelope.target_addr,
            kind=envelope.kind,
            payload=envelope.payload,
            source=envelope.source_addr or str(envelope.meta.get("source", "") or ""),
            summary=envelope.summary,
        )

    def route_inbound(self, envelope: RouteEnvelope):
        if not envelope.target_addr:
            return {"ok": False, "error": "missing target_addr"}
        inbox = self.adapt_to_inbox(envelope)
        self.hub.submit(inbox)
        self._audit(
            envelope.target_addr,
            envelope.summary or envelope.kind,
            "inbound",
            envelope.agent_key,
        )
        return inbox

    # ── 解析：语义目标 → 地址列表 ──
    def resolve(self, to: Any, ctx: Optional[Dict[str, Any]] = None) -> List[str]:
        ctx = ctx or {}
        source = (ctx.get("source_addr") or "").strip()
        if to is None:
            return [source] if source else []
        if isinstance(to, str):
            items = [to]
        elif isinstance(to, (list, tuple, set)):
            items = list(to)
        else:
            items = [str(to)]

        out: List[str] = []
        for it in items:
            s = (it or "").strip()
            if not s:
                continue
            if s in ("reply", "source"):          # 回来源
                if source:
                    out.append(source)
                continue
            out.append(s)
        if not out and source:
            out = [source]
        # 去重保序
        seen, uniq = set(), []
        for a in out:
            if a not in seen:
                seen.add(a)
                uniq.append(a)
        return uniq

    # ── 投递：地址列表 → 各通道 ──
    def deliver(
        self,
        targets: List[str],
        text: str,
        *,
        source_addr: str = "",
        agent_key: str = "default",
        hop: int = 0,
    ) -> Dict[str, Any]:
        text = (text or "").strip()
        sent: List[str] = []
        skipped: List[str] = []
        failed: List[str] = []
        reasons: Dict[str, str] = {}

        if not text:
            return {"ok": False, "error": "empty text",
                    "sent": sent, "skipped": skipped, "failed": failed, "reasons": reasons}
        if hop > MAX_HOP:
            return {"ok": False, "error": f"hop>{MAX_HOP}（防环）",
                    "sent": sent, "skipped": skipped, "failed": failed, "reasons": reasons}
        if not targets:
            return {"ok": False, "error": "no target（且无来源可回）",
                    "sent": sent, "skipped": skipped, "failed": failed, "reasons": reasons}

        reg = self._registry()
        manager = self._manager()
        for addr in targets:
            channel, target = parse_addr(addr) if ":" in addr else ("", addr)
            # 护栏：端点 / 通道可发性
            allowed, reason = manager.can_send_text(addr)
            if not allowed:
                skipped.append(addr)
                reasons[addr] = reason
                self._audit(addr, text, f"denied:{reason}", agent_key)
                continue
            # 护栏：ACL
            if not self.acl.allow(agent_key, addr):
                skipped.append(addr)
                reasons[addr] = "acl_denied"
                self._audit(addr, text, "denied:acl", agent_key)
                continue
            # 护栏：限速
            if not self.rate.allow(addr):
                skipped.append(addr)
                reasons[addr] = "rate_limited"
                self._audit(addr, text, "denied:rate", agent_key)
                continue
            # 取通道并投递
            ch = reg.get(channel) if channel else None
            if ch is None:
                failed.append(addr)
                reasons[addr] = "no_channel"
                self._audit(addr, text, "failed:no_channel", agent_key)
                continue
            try:
                ok = bool(ch.send(target, text))
            except Exception as e:  # noqa: BLE001
                logger.exception("[Router] send failed addr=%s", addr)
                ok = False
                reasons[addr] = f"exception:{e}"
            if ok:
                sent.append(addr)
                self._audit(addr, text, "sent", agent_key)
                # 出站镜像：只对「别的会话」（回来源由本轮回复负责）
                if self.mirror and addr != source_addr:
                    try:
                        self.mirror(addr, text)
                    except Exception:  # noqa: BLE001
                        logger.exception("[Router] mirror failed addr=%s", addr)
            else:
                failed.append(addr)
                reasons.setdefault(addr, "send_failed")
                self._audit(addr, text, "failed", agent_key)

        return {
            "ok": bool(sent) and not failed,
            "sent": sent, "skipped": skipped, "failed": failed, "reasons": reasons,
        }

    def route_outbound(self, envelope: RouteEnvelope, *, hop: int = 0) -> Dict[str, Any]:
        blocks = list(envelope.blocks or [])
        preview = self._block_preview(blocks)
        sent: List[str] = []
        skipped: List[str] = []
        failed: List[str] = []
        reasons: Dict[str, str] = {}

        if not blocks:
            return {"ok": False, "error": "empty envelope", "sent": sent, "skipped": skipped, "failed": failed, "reasons": reasons}
        if hop > MAX_HOP:
            return {"ok": False, "error": f"hop>{MAX_HOP}（防环）", "sent": sent, "skipped": skipped, "failed": failed, "reasons": reasons}

        targets = self.resolve(envelope.to or envelope.target_addr, {"source_addr": envelope.source_addr})
        if not targets:
            return {"ok": False, "error": "no target（且无来源可回）", "sent": sent, "skipped": skipped, "failed": failed, "reasons": reasons}

        reg = self._registry()
        manager = self._manager()
        for addr in targets:
            channel, target = self._split_addr(addr)
            allowed, reason = manager.can_send_text(addr)
            if not allowed:
                skipped.append(addr)
                reasons[addr] = reason
                self._audit(addr, preview, f"denied:{reason}", envelope.agent_key)
                continue
            if not self.acl.allow(envelope.agent_key, addr):
                skipped.append(addr)
                reasons[addr] = "acl_denied"
                self._audit(addr, preview, "denied:acl", envelope.agent_key)
                continue
            if not self.rate.allow(addr):
                skipped.append(addr)
                reasons[addr] = "rate_limited"
                self._audit(addr, preview, "denied:rate", envelope.agent_key)
                continue

            ch = reg.get(channel) if channel else None
            if ch is None:
                failed.append(addr)
                reasons[addr] = "no_channel"
                self._audit(addr, preview, "failed:no_channel", envelope.agent_key)
                continue

            policy = manager.resolve_delivery_policy(addr, ch)
            delivery = envelope.delivery
            if delivery not in ("reply", "send"):
                delivery = "reply"
            if not (
                envelope.reply_to
                and policy.get("supports_reply")
                and policy.get("allow_reply")
                and policy.get("prefer_reply")
            ):
                delivery = "send"
            try:
                ok = self._send_blocks_via_channel(
                    ch,
                    target=target,
                    blocks=blocks,
                    delivery=delivery,
                    reply_to=(envelope.reply_to if delivery == "reply" else None),
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("[Router] envelope send failed addr=%s", addr)
                ok = False
                reasons[addr] = f"exception:{e}"
            if ok:
                sent.append(addr)
                action = "replied" if delivery == "reply" else "sent"
                self._audit(addr, preview, action, envelope.agent_key)
                if self.mirror and addr != envelope.source_addr:
                    try:
                        self.mirror(addr, preview)
                    except Exception:  # noqa: BLE001
                        logger.exception("[Router] mirror failed addr=%s", addr)
            else:
                failed.append(addr)
                reasons.setdefault(addr, "send_failed")
                self._audit(addr, preview, "failed", envelope.agent_key)

        return {"ok": bool(sent) and not failed, "sent": sent, "skipped": skipped, "failed": failed, "reasons": reasons}

    def add_reaction(self, source_addr: str, message_id: str) -> str:
        if not source_addr or not message_id:
            return ""
        channel, _target = self._split_addr(source_addr)
        ch = self._registry().get(channel) if channel else None
        fn = getattr(ch, "add_reaction", None)
        if not callable(fn):
            return ""
        try:
            return fn(message_id) or ""
        except Exception:  # noqa: BLE001
            logger.exception("[Router] add_reaction failed source=%s", source_addr)
            return ""

    def delete_reaction(self, source_addr: str, message_id: str, reaction_id: str) -> bool:
        if not source_addr or not message_id or not reaction_id:
            return False
        channel, _target = self._split_addr(source_addr)
        ch = self._registry().get(channel) if channel else None
        fn = getattr(ch, "delete_reaction", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(message_id, reaction_id))
        except Exception:  # noqa: BLE001
            logger.exception("[Router] delete_reaction failed source=%s", source_addr)
            return False

    def send(
        self,
        to: Any,
        text: str,
        *,
        ctx: Optional[Dict[str, Any]] = None,
        agent_key: str = "default",
        hop: int = 0,
    ) -> Dict[str, Any]:
        ctx = ctx or {}
        return self.submit(
            RouteEnvelope(
                direction="outbound",
                kind="text",
                delivery="send",
                to=self.resolve(to, ctx),
                source_addr=(ctx.get("source_addr") or ""),
                text=text,
                agent_key=agent_key,
            ),
            hop=hop,
        )

    def _audit(self, addr: str, text: str, action: str, agent_key: str) -> None:
        self.auditor.record({
            "ts": time.time(), "agent": agent_key, "addr": addr,
            "action": action, "len": len(text or ""), "preview": (text or "")[:80],
        })


# ── 模块级单例 ─────────────────────────────────────────────
ROUTER = Router()


def get_router() -> Router:
    return ROUTER


def send(to: Any, text: str, **kw) -> Dict[str, Any]:
    return ROUTER.send(to, text, **kw)


__all__ = ["Router", "Acl", "RateLimiter", "Auditor", "ROUTER", "get_router", "send", "MAX_HOP"]
