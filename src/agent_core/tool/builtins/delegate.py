import uuid
from typing import Any, Dict

from agent_core.config import AgentConfig

from ..models import ToolResult

_SUBAGENT_PRESETS: Dict[str, Dict[str, Any]] = {
    "general": {
        "description": "通用子代理，可执行多步任务、搜索代码、读写文件",
        "tools": None,
    },
    "explore": {
        "description": "快速只读探索，用于搜索代码库、查找文件、回答问题",
        "tools": ["terminal", "skills_list", "use_skill", "read"],
    },
}

_BLOCKED_SUBAGENT_TOOLS = {"delegate_task", "ask_user", "skill_install"}


def _parent_target(ctx: Dict[str, Any]) -> str:
    """父端点地址（用于子任务完成后回报）。"""
    cc = (ctx or {}).get("channel_ctx") or {}
    chat_id = cc.get("chat_id") or ""
    platform = cc.get("platform") or "feishu"
    return f"{platform}:{chat_id}" if chat_id else ""


def delegate_task(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    sub_type = (args.get("subagent_type") or "general").strip()
    prompt = (args.get("prompt") or "").strip()
    description = (args.get("description") or prompt[:50]).strip()
    run_in_background = args.get("run_in_background", True)
    if not prompt:
        return ToolResult(ok=False, error="缺少 prompt 参数")
    agent = ctx.get("agent")
    if agent is None:
        return ToolResult(ok=False, error="Agent 不可用")
    delegate_depth = ctx.get("delegate_depth", 0)
    if delegate_depth >= 2:
        return ToolResult(
            ok=False,
            error=f"子代理嵌套深度已达上限 ({delegate_depth})，不能再创建子代理",
            signal="__continue__",
        )
    preset = _SUBAGENT_PRESETS.get(sub_type)
    if preset is None:
        available = ", ".join(_SUBAGENT_PRESETS.keys())
        return ToolResult(ok=False, error=f"未知子代理类型: {sub_type}。可用: {available}")
    yield_event = ctx.get("yield_event")
    config = ctx.get("config")
    sub_name = f"sub_{uuid.uuid4().hex[:8]}"
    sub_sys_prompt = (
        f"你是一个专门的 {sub_type} 子代理。任务: {description}\n\n"
        f"{config.sys_prompt if config else ''}"
    )
    sub_config = AgentConfig(
        base_url=config.base_url if config else "",
        sys_prompt=sub_sys_prompt,
        api_spec=config.api_spec if config else {},
        verbose=config.verbose if config else False,
    )
    sub = agent.create_sub_agent(sub_name)
    if preset.get("tools") is not None:
        allowed = set(preset["tools"])
        for tname in list(sub.tool_registry.list_tools()):
            if tname not in allowed:
                sub.tool_registry.unregister(tname)
    for tname in _BLOCKED_SUBAGENT_TOOLS:
        try:
            sub.tool_registry.unregister(tname)
        except Exception:
            pass
    child_ctx = {**ctx, "delegate_depth": delegate_depth + 1}
    if yield_event:
        yield_event("subagent_start", {
            "name": sub_name, "type": sub_type, "description": description,
        })
    if run_in_background:
        from agent_core.runtime.subagent_pool import get_pool

        async def _run_bg():
            _reply = ""
            async for _ev_type, _ev_data in sub.chat_stream_events(
                user_message=prompt, session_id=None, config=sub_config,
            ):
                if _ev_type == "done":
                    _reply = (_ev_data or {}).get("reply", "")
            return _reply

        run_id = get_pool().submit(
            target=_parent_target(ctx),
            description=description,
            run_fn=_run_bg,
            kind=sub_type,
        )
        return ToolResult(
            ok=True,
            data=f"子任务 [{description}] 已提交后台执行（run_id={run_id}），完成后会在边界告知你。",
            signal="__continue__",
        )
    import asyncio as _asyncio_for_sub

    async def _run_sub_agent():
        _reply = ""
        async for _ev_type, _ev_data in sub.chat_stream_events(
            user_message=prompt, session_id=None, config=sub_config,
        ):
            if _ev_type == "text_stream" and yield_event:
                yield_event("subagent_text", {
                    "name": sub_name, "content": _ev_data.get("content", ""),
                })
            elif _ev_type == "done":
                _reply = _ev_data.get("reply", "")
        return _reply

    final_reply = _asyncio_for_sub.run(_run_sub_agent())
    if yield_event:
        yield_event("subagent_result", {
            "name": sub_name, "type": sub_type, "reply": final_reply[:500],
        })
    return ToolResult(
        ok=True,
        data=f"子任务 [{description}] 完成:\n{final_reply}",
        signal="__continue__",
    )


DELEGATE_TASK_DEF = {
    "type": "object",
    "properties": {
        "subagent_type": {
            "type": "string",
            "description": "子代理类型: general（通用，默认）或 explore（只读探索）",
            "enum": ["general", "explore"],
        },
        "description": {
            "type": "string",
            "description": "简短的任务描述（3-5 个词）",
        },
        "prompt": {
            "type": "string",
            "description": "子代理的详细指令",
        },
        "run_in_background": {
            "type": "boolean",
            "description": "是否后台运行（默认 true：立即返回 run_id、不等待，完成后在边界回报）。"
                           "设 false 则同步等待子代理结果。",
        },
    },
    "required": ["prompt"],
}
