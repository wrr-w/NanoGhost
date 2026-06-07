"""Agent 生命周期钩子系统。

双层设计：
1. HookBus — 事件驱动的 Hook 总线，支持外部通过 on() 注册回调
2. AgentHooks — 向后兼容的基类，支持继承 + 覆写方法

两者共存：Agent 主循环先触发 HookBus（外部注册的回调），
再调用 AgentHooks 的方法（如果传入了 hooks= 参数）。
"""

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("agent_core")


# ──────────────────────────────────────────
# 事件名常量
# ──────────────────────────────────────────

HOOK_EVENTS = frozenset({
    "before_llm_call",
    "after_llm_call",
    "before_tool_dispatch",
    "after_tool_dispatch",
    "before_response",
    "on_error",
})


# ──────────────────────────────────────────
# HookBus — 事件驱动的 Hook 总线
# ──────────────────────────────────────────

class HookBus:
    """事件驱动的 Hook 总线。

    允许外部通过 on() 注册回调，在主循环的固定节点通过 emit() 触发。
    """

    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = {e: [] for e in HOOK_EVENTS}

    def on(self, event: str, fn: Callable) -> None:
        """注册一个 hook 回调。"""
        if event not in HOOK_EVENTS:
            raise ValueError(
                f"Unknown hook event: {event!r}. "
                f"Valid events: {sorted(HOOK_EVENTS)}"
            )
        self._handlers[event].append(fn)
        logger.debug("[HookBus] registered handler for '%s'", event)

    def emit(self, event: str, **kwargs) -> List[Any]:
        """触发指定事件，返回所有非 None 的返回值。"""
        results: List[Any] = []
        for fn in self._handlers.get(event, []):
            try:
                ret = fn(**kwargs)
                if ret is not None:
                    results.append(ret)
            except Exception as e:
                logger.warning(
                    "[HookBus] '%s' callback %s raised: %s",
                    event, getattr(fn, "__name__", repr(fn)), e,
                )
        return results


# ──────────────────────────────────────────
# AgentHooks — 向后兼容的基类
# ──────────────────────────────────────────

class AgentHooks:
    """Agent 生命周期钩子（方法驱动，向后兼容）。

    所有方法默认都是空操作（返回 None），只需覆写需要的钩子即可。
    """

    def before_llm_call(self, messages, config):
        """LLM 调用前，可修改 messages。"""
        return None

    def after_llm_call(self, response, messages):
        """LLM 调用后，可修改 response。"""
        return None

    def before_tool_dispatch(self, name, args, ctx):
        """Tool 分发前，可修改参数或阻止调用。

        Returns:
            dict  — 修改后的 args
            None  — 不做变更
            False — 阻止此工具调用
        """
        return None

    def after_tool_dispatch(self, name, result, ctx):
        """Tool 分发后，可修改结果。"""
        return None

    def before_response(self, reply, steps):
        """最终回复前，可修改回复内容。"""
        return None

    def on_error(self, error):
        """发生异常时调用（纯通知）。"""
        pass
