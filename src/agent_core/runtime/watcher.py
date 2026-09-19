# -*- coding: utf-8 -*-
"""Watcher —— 事件源：盯「变化」（P4）。

和 Timer 的区别：
    Timer   : 到点 **无条件** 产事件（"时间到了"）
    Watcher : **检测到变化** 才产事件（"东西变了"）→ 需要存「上次快照」做比对

统一形态（见 docs/channels_and_router.md §8）：
    生产者（事件源）→ 事件 → 【收件箱】→ 常驻消费者 / ReAct 边界
Watcher 只是「又一个生产者」，产出的也是同一种事件，走同一条路。

用法（probe 由调用方注入，Watcher 不认识业务）：
    w = get_watcher()
    w.watch("工单待处理", probe=lambda: api("list_tickets", status="NEW"),
            target="feishu:oc_ops", interval=30)
    # probe() 返回值变化 → 往 target 收件箱投一条 kind="event" 事件
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .inbox import submit_event

logger = logging.getLogger("agent_core")

#: probe：无参，返回「可比较的快照」（dict/list/str… 会被 JSON 规范化）
ProbeFn = Callable[[], Any]


def _norm(v: Any) -> str:
    """把快照规范化成可比较的字符串（键序无关）。"""
    try:
        return json.dumps(v, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(v)


@dataclass
class Watch:
    name: str
    probe: ProbeFn
    target: str = ""
    kind: str = "event"
    interval: float = 30.0
    summary: str = ""
    last: Optional[str] = None
    last_at: float = 0.0
    armed: bool = False          # 首次探测只「装填」快照，不报变化


class Watcher:
    """轮询 + 快照比对 → 变化即产事件。"""

    def __init__(self, *, tick: float = 1.0) -> None:
        self.tick = tick
        self._watches: Dict[str, Watch] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── 注册 ──────────────────────────────────────────────
    def watch(
        self,
        name: str,
        probe: ProbeFn,
        *,
        target: str = "",
        kind: str = "event",
        interval: Optional[float] = None,
        summary: str = "",
    ) -> Watch:
        w = Watch(
            name=name, probe=probe, target=target, kind=kind,
            interval=float(interval if interval is not None else 30.0),
            summary=summary or name,
        )
        with self._lock:
            self._watches[name] = w
        logger.info("[Watcher] +watch %s target=%s interval=%ss", name, target, w.interval)
        return w

    def unwatch(self, name: str) -> bool:
        with self._lock:
            return self._watches.pop(name, None) is not None

    def list_watches(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {"name": w.name, "target": w.target, "kind": w.kind,
                 "interval": w.interval, "armed": w.armed, "last_at": w.last_at}
                for w in self._watches.values()
            ]

    # ── 生命周期 ──────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="watcher", daemon=True)
        self._thread.start()
        logger.info("[Watcher] started (%d watches)", len(self._watches))

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                self.check_once()
            except Exception:
                logger.exception("[Watcher] check loop error")
            time.sleep(self.tick)

    # ── 单次检查（测试 / 手动也用它）──────────────────────
    def check_once(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """到点则探测；发生变化则产事件。返回本次产出的事件描述列表。"""
        now = now if now is not None else time.time()
        emitted: List[Dict[str, Any]] = []
        with self._lock:
            watches = list(self._watches.values())

        for w in watches:
            if w.armed and now - w.last_at < w.interval:
                continue                      # 未到间隔 → 跳过
            w.last_at = now
            try:
                snap = _norm(w.probe())
            except Exception as e:  # noqa: BLE001
                logger.exception("[Watcher] probe failed: %s", w.name)
                snap = f"__error__:{e}"

            if not w.armed:
                w.last = snap
                w.armed = True
                continue                      # 首轮只装填，不报变化
            if snap == w.last:
                continue                      # 无变化 → 不产事件

            before = w.last
            w.last = snap
            logger.info("[Watcher] ~change %s → 产事件 target=%s", w.name, w.target)
            if w.target:
                try:
                    submit_event(
                        w.target,
                        kind=w.kind,
                        payload={"watch": w.name, "before": before, "after": snap},
                        source="watcher",
                        summary=f"变化：{w.summary}",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("[Watcher] emit failed: %s", w.name)
            self._to_sink(w, before, snap)
            emitted.append({"watch": w.name, "target": w.target, "before": before, "after": snap})

        return emitted

    @staticmethod
    def _to_sink(w: Watch, before: Optional[str], after: str) -> None:
        """结果也落 Sink（B：结果解耦）。best-effort。"""
        try:
            from agent_core.sink import get_sink
            get_sink().emit(
                "watch_change",
                payload={"watch": w.name, "before": before, "after": after},
                source="watcher", target=w.target, summary=f"变化：{w.summary}",
            )
        except Exception:
            pass


# ── 模块级单例 ─────────────────────────────────────────────
_WATCHER: Optional[Watcher] = None
_LOCK = threading.Lock()


def get_watcher() -> Watcher:
    global _WATCHER
    with _LOCK:
        if _WATCHER is None:
            _WATCHER = Watcher()
        return _WATCHER


def reset_watcher() -> None:
    global _WATCHER
    with _LOCK:
        _WATCHER = None


__all__ = ["Watch", "Watcher", "get_watcher", "reset_watcher", "ProbeFn"]
