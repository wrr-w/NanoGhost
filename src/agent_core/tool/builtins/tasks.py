# -*- coding: utf-8 -*-
"""内置工具：定时任务 —— 让 agent **自己**给自己下定时任务。

- schedule_task           新建定时任务（cron / daily_at / interval / at 四选一）
- list_scheduled_tasks    查看当前所有定时任务
- cancel_scheduled_task   取消（按 name 或 id）

任务存到 ``<实例目录>/tasks.json``，由 ``agent_core.scheduler`` 的调度循环执行；
创建后**无需重启**，最长 5 秒生效。

子 agent 继承父 agent 的工具（delegate.py 只黑名单了 delegate/ask/skill_install），
所以这些工具在子 agent 里同样可用。
"""
from typing import Any, Dict

from ..models import ToolResult

# ══════════════════════════════════════════════
# schedule_task
# ══════════════════════════════════════════════

SCHEDULE_TASK_DEF: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "任务名，如『工单巡检』『每日晨报』"},
        "prompt": {"type": "string",
                   "description": "到点时让 agent 执行的内容（就像用户对它说的话），例如"
                                  "『读 capture /api/signals 与 ticketcore NEW 工单，判读处理；无异常静默』"},
        "cron": {"type": "string",
                 "description": "5 段 cron『分 时 日 月 周』：『0 3 * * *』=每天 03:00；"
                                "『*/10 * * * *』=每 10 分钟；『0 9 * * 1-5』=工作日 09:00。"
                                "与 daily_at / interval / at 四选一"},
        "daily_at": {"type": "string",
                     "description": "简化写法『HH:MM』=每天该时刻（等价 cron）。四选一"},
        "interval": {"type": "number",
                     "description": "每 N 秒执行一次（如 600=每 10 分钟）。四选一"},
        "at": {"type": "string",
               "description": "一次性时刻 ISO，如『2026-09-20T15:00:00』。四选一"},
        "quiet": {"type": "boolean",
                  "description": "true=执行结果不回消息（默认 true，适合『干完就走』的巡检）；"
                                 "false 时必须能确定 chat_id"},
        "chat_id": {"type": "string",
                    "description": "要回消息时的目标会话 id；缺省=当前会话（若拿得到）"},
    },
    "required": ["name", "prompt"],
}


def schedule_task(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    from agent_core.scheduler import get_task_manager, TaskSpec, fmt_ts

    name = (args.get("name") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    if not name or not prompt:
        return ToolResult(ok=False, error="缺少 name 或 prompt")

    cron = (args.get("cron") or "").strip()
    daily_at = (args.get("daily_at") or "").strip()
    if not cron and daily_at:
        try:
            hh, mm = daily_at.split(":")
            cron = f"{int(mm)} {int(hh)} * * *"
        except Exception:
            return ToolResult(ok=False, error=f"daily_at 应为『HH:MM』，收到 {daily_at!r}")
    interval = float(args.get("interval") or 0)
    at = (args.get("at") or "").strip()
    if not (cron or interval or at):
        return ToolResult(ok=False, error="必须提供 cron / daily_at / interval / at 之一")

    quiet = args.get("quiet", True)
    channel_ctx = ctx.get("channel_ctx") or {}
    chat_id = (args.get("chat_id") or "").strip() or str(channel_ctx.get("chat_id") or "")

    mgr = get_task_manager()
    spec = TaskSpec(name=name, prompt=prompt, quiet=bool(quiet), chat_id=chat_id,
                    cron=cron, interval=interval, at=at, enabled=True)
    if not spec.valid():
        why = ("cron 格式须为『分 时 日 月 周』5 段" if cron and not spec.schedule_kind()
               else "quiet=false 时必须能确定 chat_id（请显式给 chat_id）")
        return ToolResult(ok=False, error=f"任务无效：{why}")

    old = mgr.find(name)
    if old:                                  # 同名 → 覆盖
        mgr.remove(old.id)
    spec = mgr.add(spec)
    return ToolResult(ok=True, data={
        "ok": True,
        "task": spec.to_public(),
        "next_run_at": fmt_ts(spec.next_run_at),
        "note": "已加入调度（无需重启，最长 5 秒生效）",
    })


# ══════════════════════════════════════════════
# list_scheduled_tasks
# ══════════════════════════════════════════════

LIST_SCHEDULED_TASKS_DEF: Dict[str, Any] = {
    "type": "object",
    "properties": {},
}


def list_scheduled_tasks(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    from agent_core.scheduler import get_task_manager
    mgr = get_task_manager()
    return ToolResult(ok=True, data={"ok": True, "count": len(mgr.tasks),
                                     "tasks": mgr.snapshot()})


# ══════════════════════════════════════════════
# cancel_scheduled_task
# ══════════════════════════════════════════════

CANCEL_SCHEDULED_TASK_DEF: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "任务名或任务 id"},
    },
    "required": ["name"],
}


def cancel_scheduled_task(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    from agent_core.scheduler import get_task_manager
    ident = (args.get("name") or args.get("id") or "").strip()
    if not ident:
        return ToolResult(ok=False, error="缺少 name")
    mgr = get_task_manager()
    if mgr.remove(ident):
        return ToolResult(ok=True, data={"ok": True, "removed": ident,
                                         "remaining": len(mgr.tasks)})
    return ToolResult(ok=False, error=f"没找到任务：{ident}")
