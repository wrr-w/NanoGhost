# -*- coding: utf-8 -*-
"""定时任务（Scheduler）—— agent 可**自助**创建/管理定时任务。

设计
----
· 触发方式（P1）：到点后往【收件箱】丢一条 ``timer`` 事件，由**常驻消费者**
  与通道消息走**同一条路**消费（不再自己起轮）。因此不经过飞书、不需要外部唤醒。
· 任务存储：``<实例目录>/tasks.json``（持久化，重启不丢）。
· 动态生效：agent 用内置工具 ``schedule_task`` / ``list_scheduled_tasks`` /
  ``cancel_scheduled_task`` 增删任务，调度循环**最长 5 秒**内自动拾取，无需重启。
· 子 agent：这些工具是普通内置工具，子 agent 继承父工具（除非在黑名单），
  所以子 agent 也能给自己/父级下定时任务。

任务定义（tasks.json）
---------------------
[
  {
    "name": "工单巡检",
    "enabled": true,
    "prompt": "读 capture /api/signals + ticketcore NEW 工单，判读处理；无异常静默",
    "quiet": true,                          // true=不回消息（干完就走）
    "chat_id": "",                          // quiet=false 时要给
    "cron": "0 3 * * *",                    // 分 时 日 月 周（三选一）
    "interval": 0,                          // 每 N 秒（三选一）
    "at": ""                                // 一次性 ISO 时刻（三选一）
  }
]

env 兜底（没有 tasks.json 时）：AGENT_TASK_ENABLED / AGENT_TASK_INTERVAL /
AGENT_TASK_CHAT_ID / AGENT_TASK_PROMPT / AGENT_TASK_QUIET / AGENT_TASK_NAME
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_core.channel.interfaces import ChannelIO
from agent_core.channel.message_context import MessageSource, MessageContext
from agent_core.presenter import run_agent_turn

logger = logging.getLogger("agent_core")

_TRUTHY = ("1", "true", "yes", "on")
_POLL_SECONDS = 5.0          # 调度循环的检查粒度（也是「改完多久生效」的上限）


# ══════════════════════════════════════════════
# cron 解析
# ══════════════════════════════════════════════

def _parse_cron_field(field: str, lo: int, hi: int) -> set:
    """解析 cron 单字段 → 允许值集合。支持 * , - / 。"""
    out: set = set()
    for part in str(field).split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, _, s = part.partition("/")
            try:
                step = max(1, int(s))
            except ValueError:
                step = 1
        if part in ("*", "?", ""):
            a, b = lo, hi
        elif "-" in part:
            x, _, y = part.partition("-")
            try:
                a, b = int(x), int(y)
            except ValueError:
                continue
        else:
            try:
                a = b = int(part)
            except ValueError:
                continue
        for v in range(a, b + 1, step):
            if lo <= v <= hi:
                out.add(v)
    return out


def cron_valid(expr: str) -> bool:
    return len(str(expr).split()) == 5


def cron_next(expr: str, after_ts: float) -> Optional[float]:
    """返回 after_ts 之后的**下一个**触发时刻（时间戳）；无则 None。

    cron 5 段：分 时 日 月 周（周：0/7=周日, 1=周一 … 6=周六）。
    """
    fields = str(expr).split()
    if len(fields) != 5:
        return None
    mins = _parse_cron_field(fields[0], 0, 59)
    hours = _parse_cron_field(fields[1], 0, 23)
    doms = _parse_cron_field(fields[2], 1, 31)
    months = _parse_cron_field(fields[3], 1, 12)
    dows = _parse_cron_field(fields[4], 0, 7)
    if 7 in dows:
        dows.add(0)
    dom_restricted = fields[2].strip() not in ("*", "?")
    dow_restricted = fields[4].strip() not in ("*", "?")
    if not mins or not hours or not months:
        return None

    dt = datetime.fromtimestamp(after_ts).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        cron_dow = (dt.weekday() + 1) % 7        # Mon=0..Sun=6 → cron: Sun=0..Sat=6
        day_ok = True
        if dom_restricted or dow_restricted:
            d_ok = dt.day in doms
            w_ok = cron_dow in dows
            if dom_restricted and dow_restricted:
                day_ok = d_ok or w_ok            # cron 惯例：都限定时取 OR
            elif dom_restricted:
                day_ok = d_ok
            else:
                day_ok = w_ok
        if day_ok and dt.minute in mins and dt.hour in hours and dt.month in months:
            return dt.timestamp()
        dt += timedelta(minutes=1)
    return None


def fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════════════════════════════════
# 任务定义
# ══════════════════════════════════════════════

@dataclass
class TaskSpec:
    id: str = ""
    name: str = "task"
    enabled: bool = True
    prompt: str = ""
    quiet: bool = True
    chat_id: str = ""
    cron: str = ""              # 三选一
    interval: float = 0.0       # 三选一
    at: str = ""                # 三选一（一次性）
    # ── 运行时（不持久化）──
    created_at: float = 0.0
    last_run_at: float = 0.0
    next_run_at: float = 0.0
    run_count: int = 0

    # -- 校验 --
    def valid(self) -> bool:
        if not self.prompt.strip() or not self.schedule_kind():
            return False
        if not self.quiet and not self.chat_id:
            return False
        return True

    def schedule_kind(self) -> str:
        if self.cron.strip():
            return "cron" if cron_valid(self.cron) else ""
        if self.interval and self.interval > 0:
            return "interval"
        if self.at.strip():
            return "at"
        return ""

    @property
    def session_key(self) -> str:
        """quiet 且无真实会话时用伪 key（只影响 session，不发消息）。"""
        return self.chat_id or f"sched:{self.name}"

    # -- 下一次触发 --
    def compute_next(self, now: Optional[float] = None) -> Optional[float]:
        now = time.time() if now is None else now
        kind = self.schedule_kind()
        if kind == "cron":
            return cron_next(self.cron, now)
        if kind == "interval":
            return now + max(1.0, self.interval)
        if kind == "at":
            try:
                ts = datetime.fromisoformat(self.at).timestamp()
            except ValueError:
                return None
            return ts if ts > now else None
        return None

    # -- 序列化 --
    def to_store(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "enabled": self.enabled,
            "prompt": self.prompt, "quiet": self.quiet, "chat_id": self.chat_id,
            "cron": self.cron, "interval": self.interval, "at": self.at,
        }

    def to_public(self) -> Dict[str, Any]:
        d = self.to_store()
        d.update({
            "schedule": self.schedule_kind(),
            "next_run_at": fmt_ts(self.next_run_at),
            "last_run_at": fmt_ts(self.last_run_at),
            "run_count": self.run_count,
        })
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskSpec":
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:8]),
            name=str(d.get("name") or "task"),
            enabled=bool(d.get("enabled", True)),
            prompt=str(d.get("prompt") or ""),
            quiet=bool(d.get("quiet", True)),
            chat_id=str(d.get("chat_id") or ""),
            cron=str(d.get("cron") or ""),
            interval=float(d.get("interval") or 0),
            at=str(d.get("at") or ""),
            created_at=float(d.get("created_at") or time.time()),
        )


# ══════════════════════════════════════════════
# 任务管理（单例）
# ══════════════════════════════════════════════

def _default_path() -> str:
    inst = os.environ.get("INSTANCE_DIR", "") or "."
    return os.path.join(inst, "tasks.json")


class TaskManager:
    """任务清单（内存 + tasks.json 持久化）。同进程单例，工具与调度循环共用。"""

    def __init__(self, path: str = "") -> None:
        self.path = path or _default_path()
        self.tasks: List[TaskSpec] = []
        self.load()

    # -- 载入/保存 --
    def load(self) -> None:
        self.tasks = []
        p = Path(self.path)
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    raw = raw.get("tasks", [])
                for item in raw or []:
                    if isinstance(item, dict):
                        self.tasks.append(TaskSpec.from_dict(item))
            except Exception:
                logger.exception("[Scheduler] tasks.json 解析失败: %s", p)
        # env 兜底（没有文件时）
        if not self.tasks and str(os.environ.get("AGENT_TASK_ENABLED", "")).strip().lower() in _TRUTHY:
            self.tasks.append(TaskSpec.from_dict({
                "name": os.environ.get("AGENT_TASK_NAME", "default"),
                "enabled": True,
                "prompt": os.environ.get("AGENT_TASK_PROMPT", ""),
                "quiet": str(os.environ.get("AGENT_TASK_QUIET", "")).strip().lower() in _TRUTHY,
                "chat_id": os.environ.get("AGENT_TASK_CHAT_ID", ""),
                "interval": float(os.environ.get("AGENT_TASK_INTERVAL", "120") or 120),
            }))
        now = time.time()
        dropped = 0
        for t in self.tasks:
            if not t.valid():
                dropped += 1
                continue
            t.next_run_at = t.compute_next(now) or 0.0
        logger.info("[Scheduler] 载入 %d 个任务（丢弃无效 %d）", len(self.tasks) - dropped, dropped)

    def save(self) -> None:
        try:
            p = Path(self.path)
            p.parent.mkdir(parents=True, exist_ok=True)
            data = [t.to_store() for t in self.tasks]
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("[Scheduler] 写 tasks.json 失败: %s", self.path)

    # -- 增删改查 --
    def add(self, spec: TaskSpec) -> TaskSpec:
        if not spec.id:
            spec.id = uuid.uuid4().hex[:8]
        spec.created_at = spec.created_at or time.time()
        spec.next_run_at = spec.compute_next() or 0.0
        self.tasks.append(spec)
        self.save()
        return spec

    def find(self, ident: str) -> Optional[TaskSpec]:
        ident = (ident or "").strip()
        for t in self.tasks:
            if ident and (t.id == ident or t.name == ident):
                return t
        return None

    def remove(self, ident: str) -> bool:
        t = self.find(ident)
        if not t:
            return False
        self.tasks.remove(t)
        self.save()
        return True

    def set_enabled(self, ident: str, enabled: bool) -> bool:
        t = self.find(ident)
        if not t:
            return False
        t.enabled = bool(enabled)
        if t.enabled:
            t.next_run_at = t.compute_next() or 0.0
        self.save()
        return True

    # -- 调度 --
    def due(self, now: float) -> List[TaskSpec]:
        return [t for t in self.tasks
                if t.enabled and t.next_run_at and t.next_run_at <= now and t.valid()]

    def mark_ran(self, t: TaskSpec, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        t.last_run_at = now
        t.run_count += 1
        nxt = t.compute_next(now)
        if nxt:
            t.next_run_at = nxt
        else:                       # 一次性任务跑完 → 停用
            t.next_run_at = 0.0
            t.enabled = False
        self.save()

    def seconds_to_next(self, now: float) -> Optional[float]:
        nxt = [t.next_run_at for t in self.tasks if t.enabled and t.next_run_at]
        if not nxt:
            return None
        return max(0.0, min(nxt) - now)

    def snapshot(self) -> List[Dict[str, Any]]:
        return [t.to_public() for t in self.tasks]


_MANAGER: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = TaskManager()
    return _MANAGER


def reset_task_manager() -> None:
    global _MANAGER
    _MANAGER = None


# ══════════════════════════════════════════════
# 静默 IO
# ══════════════════════════════════════════════

class NullIO(ChannelIO):
    """丢弃一切渠道输出（定时任务专用）。

    agent 的 MCP 工具调用不经过这里 → 建单/通知/改状态照常；
    只是这一轮的**文本回复**不往群里发（避免每 N 分钟刷屏）。
    """

    def send_text(self, chat_id: str, text: str) -> bool:      # noqa: D102
        return True

    def reply(self, message_id: str, text: str) -> bool:        # noqa: D102
        return True

    def send_images(self, chat_id: str, b64_list: List[str]) -> Dict[str, Any]:  # noqa: D102
        return {"ok": True}

    def add_reaction(self, message_id: str) -> str:             # noqa: D102
        return ""

    def delete_reaction(self, message_id: str, reaction_id: str) -> bool:  # noqa: D102
        return True

    def download_image(self, message_id: str, file_key: str):   # noqa: D102
        return None


# ══════════════════════════════════════════════
# 执行
# ══════════════════════════════════════════════

async def run_task_once(client, task: TaskSpec) -> bool:
    """跑一次任务：合成一条「消息」→ 直接进 agent 那一轮。"""
    cb = client._context_builder
    chat_id = task.session_key
    source = MessageSource(
        platform="feishu",
        chat_id=chat_id,
        chat_name=f"定时任务·{task.name}",
        chat_type="p2p",
        sender_id="__scheduler__",
        sender_name=f"定时任务:{task.name}",
        is_bot=True,
    )
    ctx = MessageContext(text=task.prompt, message_type="text", message_id="")
    user_text = cb.build_user_message(source, ctx)
    io = NullIO() if task.quiet else client.io

    logger.info("[Scheduler] ▶ 任务『%s』开始 (quiet=%s session=%s)", task.name, task.quiet, chat_id)
    t0 = time.time()
    try:
        await run_agent_turn(
            agent=client.agent,
            identity=client.instance,
            sessions=client.sessions,
            io=io,
            context_builder=cb,
            source=source,
            ctx=ctx,
            user_text=user_text,
            images_base64=None,
            base_url=client._base_url,
            api_spec=client._api_spec,
            channel_ctx={"chat_id": task.chat_id, "platform": "feishu",
                         "scheduled": True, "task_name": task.name},
        )
        logger.info("[Scheduler] ✔ 任务『%s』完成 %.0fs", task.name, time.time() - t0)
        return True
    except Exception:
        logger.exception("[Scheduler] ✘ 任务『%s』异常", task.name)
        return False


async def scheduler_loop(client) -> None:
    """定时**生产器**（P1）：到点 → 往收件箱【丢事件】（不再自己起轮）。

    与渠道 run_forever 并行运行；任务动态增减（最长 5s 生效）。
    实际执行由常驻消费者按端点忙闲完成（见 runtime/consumer.py）。
    """
    from agent_core.runtime.inbox import submit_event

    mgr = get_task_manager()
    logger.info("[Scheduler] 启动，%d 个任务：%s", len(mgr.tasks),
                "、".join(f"{t.name}@{t.schedule_kind()}" for t in mgr.tasks) or "（空）")
    while True:
        now = time.time()
        for t in mgr.due(now):
            try:
                submit_event(
                    target=f"feishu:{t.session_key}",
                    kind="timer",
                    payload={"task": t},
                    source="timer",
                    summary=f"定时任务 {t.name}",
                )
            except Exception:
                logger.exception("[Scheduler] submit failed task=%s", t.name)
            mgr.mark_ran(t)
        nxt = mgr.seconds_to_next(time.time())
        await asyncio.sleep(min(_POLL_SECONDS, nxt) if nxt is not None else _POLL_SECONDS)
