# -*- coding: utf-8 -*-
"""子 Agent 池（P3）：后台运行 · 可并行 · 上限 N · 排队 · 完成回报。

    submit(target, description, run_fn) -> run_id     立即返回
    · run_fn  : 无参函数；返回结果（async 协程或普通值都行，在 worker 线程里执行）
    · 上限    : max_concurrent（默认 3，env SUBAGENT_MAX_CONCURRENT 可覆盖）
    · 排队    : 超出上限的按 FIFO 等位
    · 完成回报: 跑完 → 更新运行表 + 往【父端点】收件箱投一条 kind="subagent_done" 事件
                → 父 agent 若忙：ReAct 边界 drain 看到；若闲：常驻消费者唤醒

运行表存【内存】（进程级）；进程重启即清空（符合「一次执行，非持久」的定位）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("agent_core")

#: run_fn：无参，可返回 awaitable 或普通值
RunFn = Callable[[], Any]


def _default_max_concurrent() -> int:
    try:
        return max(1, int(os.environ.get("SUBAGENT_MAX_CONCURRENT", "3")))
    except Exception:
        return 3


class SubAgentPool:
    """子 agent 后台执行池（线程级并发 + FIFO 排队）。"""

    def __init__(self, max_concurrent: Optional[int] = None) -> None:
        self.max_concurrent = max_concurrent or _default_max_concurrent()
        self._runs: Dict[str, Dict[str, Any]] = {}   # run_id -> 运行记录
        self._pending: deque = deque()               # 排队中的 run_id（FIFO）
        self._running: set = set()                   # 正在跑的 run_id
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._stop = False

    # ── 生命周期 ──────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._manager, name="subagent-pool", daemon=True)
        self._thread.start()
        logger.info("[SubAgentPool] started (max_concurrent=%s)", self.max_concurrent)

    def stop(self) -> None:
        self._stop = True
        with self._cv:
            self._cv.notify_all()

    # ── 提交 ──────────────────────────────────────────────
    def submit(self, target: str, description: str, run_fn: RunFn, *, kind: str = "general") -> str:
        """提交一个后台子任务 → 立即返回 run_id。"""
        run_id = "sub_" + uuid.uuid4().hex[:8]
        rec = {
            "run_id": run_id,
            "target": target or "",
            "description": description or "",
            "kind": kind or "general",
            "status": "queued",
            "created_at": time.time(),
            "started_at": 0.0,
            "finished_at": 0.0,
            "result": None,
            "error": None,
            "_fn": run_fn,
        }
        with self._cv:
            self._runs[run_id] = rec
            self._pending.append(run_id)
            self._cv.notify()
        logger.info("[SubAgentPool] +queued %s target=%s desc=%s", run_id, rec["target"], rec["description"])
        return run_id

    # ── 管理器：控并发、排队 ──────────────────────────────
    def _manager(self) -> None:
        while not self._stop:
            starters: List[str] = []
            with self._cv:
                while not self._stop and (
                    not self._pending or len(self._running) >= self.max_concurrent
                ):
                    self._cv.wait(1.0)
                while self._pending and len(self._running) < self.max_concurrent:
                    rid = self._pending.popleft()
                    self._running.add(rid)
                    starters.append(rid)
            for rid in starters:
                threading.Thread(
                    target=self._run_one, args=(rid,), daemon=True, name=f"pool-{rid}"
                ).start()

    # ── 单个执行 ──────────────────────────────────────────
    def _run_one(self, run_id: str) -> None:
        rec = self._runs.get(run_id)
        if rec is None:
            return
        rec["status"] = "running"
        rec["started_at"] = time.time()
        result, error = None, None
        try:
            val = rec["_fn"]()
            if inspect.isawaitable(val):
                val = asyncio.run(val)
            result = val
            rec["status"] = "done"
        except Exception as e:  # noqa: BLE001
            error = str(e)
            rec["status"] = "error"
            logger.exception("[SubAgentPool] run failed %s", run_id)
        finally:
            rec["result"] = result
            rec["error"] = error
            rec["finished_at"] = time.time()
            rec.pop("_fn", None)
            with self._cv:
                self._running.discard(run_id)
                self._cv.notify()
            # ── 结果落 Sink（B：结果解耦；渠道是订阅者）──
            try:
                from agent_core.sink import get_sink
                get_sink().emit(
                    "subagent_done",
                    payload={"run_id": run_id, "description": rec["description"],
                             "status": rec["status"], "result": result, "error": error},
                    source="subagent", target=rec["target"],
                    summary=f"子任务 {rec['description']} {rec['status']}",
                )
            except Exception:  # noqa: BLE001
                logger.debug("[SubAgentPool] sink emit skipped")
            # ── 完成回报：往父端点收件箱投事件 ──
            if rec["target"]:
                try:
                    from agent_core.channel.route import RouteEnvelope
                    from agent_core.router import get_router

                    get_router().submit(
                        RouteEnvelope(
                            direction="inbound",
                            kind="subagent_done",
                            target_addr=rec["target"],
                            source_addr="subagent",
                            payload={
                                "run_id": run_id,
                                "description": rec["description"],
                                "status": rec["status"],
                                "result": result,
                                "error": error,
                            },
                            summary=f"子任务 {rec['description']} {rec['status']}",
                        )
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("[SubAgentPool] emit completion failed %s", run_id)
            logger.info("[SubAgentPool] ✔ %s %s", run_id, rec["status"])

    # ── 观测 ──────────────────────────────────────────────
    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        rec = self._runs.get(run_id)
        if not rec:
            return None
        return {k: v for k, v in rec.items() if not k.startswith("_")}

    def list_runs(self, target: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            recs = [{k: v for k, v in r.items() if not k.startswith("_")} for r in self._runs.values()]
        if target:
            recs = [r for r in recs if r["target"] == target]
        recs.sort(key=lambda r: r.get("created_at") or 0)
        return recs

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "queued": len(self._pending),
                "running": len(self._running),
                "total": len(self._runs),
                "max_concurrent": self.max_concurrent,
            }


# ── 模块级单例 ─────────────────────────────────────────────
_POOL: Optional[SubAgentPool] = None
_POOL_LOCK = threading.Lock()


def get_pool() -> SubAgentPool:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = SubAgentPool()
            _POOL.start()
        return _POOL


def reset_pool() -> None:
    """测试用：清掉单例（不 stop 旧池）。"""
    global _POOL
    with _POOL_LOCK:
        _POOL = None


__all__ = ["SubAgentPool", "RunFn", "get_pool", "reset_pool"]
