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

from agent_core.channel.endpoint import parse_addr
from agent_core.channel.registry import get_registry

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
    def __init__(self, registry=None, *, mirror: Optional[Callable[[str, str], None]] = None) -> None:
        self.registry = registry
        self.mirror = mirror
        self.acl = Acl()
        self.rate = RateLimiter()
        self.auditor = Auditor()

    def set_mirror(self, fn: Optional[Callable[[str, str], None]]) -> None:
        self.mirror = fn

    def _registry(self):
        return self.registry or get_registry()

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
        for addr in targets:
            channel, target = parse_addr(addr) if ":" in addr else ("", addr)
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
        targets = self.resolve(to, ctx)
        return self.deliver(
            targets, text,
            source_addr=(ctx.get("source_addr") or ""),
            agent_key=agent_key, hop=hop,
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
