"""
Agent 主类：对象化、多实例、SubAgent 可派生。

每个 Agent 实例拥有：
  - 独立的端口注入（db, llm, image_port）
- 独立的 namespace（记忆隔离）
- 独立的 SkillRegistry
- 可插拔的 Hook 生命周期
- 工具注册表（ToolRegistry，Hermes 风格 function calling）
- 可创建 SubAgent（继承端口，独立 namespace）

用法:
    from agent_core import Agent
    from agent_core.infra.config import AgentConfig

    agent = Agent(db=my_db, llm=my_llm, image_port=my_image, namespace="my_app")
    for ev_type, ev_data in agent.chat_stream_events(
        user_message="帮我建一个任务",
        session_id="...",
        config=AgentConfig(base_url="...", sys_prompt="...", api_spec={}),
    ):
        print(ev_type, ev_data)
"""

import logging
import asyncio
import json
import time
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections.abc import AsyncIterator


from .messages import build_agent_messages_with_history
from agent_core.tool.models import ToolCall, ToolResult
from agent_core.memory.pipeline import MemoryPipeline, MemoryTurnEvent
from agent_core.memory.postprocess import postprocess_turn

from agent_core.config import AgentConfig
from agent_core.hooks import AgentHooks, HookBus
from agent_core.interfaces import DatabasePort, ImagePort, LLMPort
from agent_core.skill import SkillRegistry, SkillDefinition
from agent_core.tool import ToolRegistry, register_builtins


_MAX_ROUNDS = 120

logger = logging.getLogger("agent_core")

# 模块级默认 db（兼容简单场景）
_default_db: Optional[DatabasePort] = None


def _get_default_db() -> DatabasePort:
    global _default_db
    if _default_db is None:
        raise RuntimeError("No default DatabasePort configured. Pass db explicitly or set agent_core.agent._default_db.")
    return _default_db


# ── P3：ReAct 边界的「事件注入」（后台完成等，忙时不打断本轮）──────
_BOUNDARY_KINDS = {"subagent_done", "event"}


def _boundary_target(channel_ctx: Optional[Dict[str, Any]]) -> str:
    cc = channel_ctx or {}
    chat_id = cc.get("chat_id") or ""
    platform = cc.get("platform") or "feishu"
    return f"{platform}:{chat_id}" if chat_id else ""


def _drain_boundary_events(channel_ctx: Optional[Dict[str, Any]]) -> List[Any]:
    """取走该端点收件箱里的「软事件」（不含通道消息 —— 那是下一轮的活）。"""
    target = _boundary_target(channel_ctx)
    if not target:
        return []
    from agent_core.runtime.inbox import get_hub
    return get_hub().drain(target, kinds=_BOUNDARY_KINDS)


def _format_boundary_events(events: List[Any]) -> str:
    lines = ["[系统事件]（agent 忙时产生，供你参考；自行决定是否处理 / 是否告知用户）"]
    for ev in events:
        p = ev.payload if isinstance(ev.payload, dict) else {}
        if ev.kind == "subagent_done":
            desc = p.get("description") or ""
            if p.get("status") == "done":
                lines.append(f"· 子任务「{desc}」完成：{(p.get('result') or '')[:800]}")
            else:
                lines.append(f"· 子任务「{desc}」失败：{p.get('error')}")
        else:
            lines.append(f"· 事件 {ev.kind}：{ev.summary or ''}")
    return "\n".join(lines)


class Agent:
    """Agent 主类。每个实例独立管理端口、记忆域、Skill、工具和 SubAgent。"""

    def __init__(
        self,
        db: DatabasePort,
        llm: LLMPort,
        image_port: Optional[ImagePort] = None,
        namespace: Optional[str] = None,
        auto_discover_skills: bool = True,
        skill_extra_dirs: Optional[List[str]] = None,
        auto_register_tools: bool = True,
        hooks: Optional[AgentHooks] = None,
    ):
        self.db = db
        self.llm = llm
        self.image_port = image_port
        self.namespace = namespace
        self._hook_bus = HookBus()
        self.hooks = hooks or AgentHooks()
        self._register_legacy_hooks()

        # Skill 系统（SKILL.md 生态）
        self.skill_registry = SkillRegistry()

        # 自动发现 SKILL.md 技能
        if auto_discover_skills:
            self.skill_registry.discover(extra_dirs=skill_extra_dirs)

        # Tool 注册表（Hermes 风格 function calling）
        self.tool_registry = ToolRegistry()
        if auto_register_tools:
            register_builtins(self.tool_registry)
            try:
                from agent_core.mcp_client import MCPManager
                from agent_core.config import load_instance_config

                inst_cfg = load_instance_config()
                fail_threshold = inst_cfg.extra.get("mcp_fail_threshold", 3)
                probe_ttl = inst_cfg.extra.get("mcp_probe_ttl_seconds", 60)

                self._mcp_manager = MCPManager(
                    fail_threshold=fail_threshold,
                    probe_ttl_seconds=probe_ttl,
                )
                self._mcp_manager.attach_tool_registry(self.tool_registry)
                import threading
                threading.Thread(target=self._mcp_manager.refresh_all, daemon=True).start()
                self._mcp_manager.start_poller()
            except Exception as _mcp_e:
                _mcp_logger = logging.getLogger('agent_core')
                _mcp_logger.exception(f'[MCP] init failed: {_mcp_e}')
                self._mcp_manager = None

        # SubAgent 管理
        self._sub_agents: Dict[str, "Agent"] = {}
        self._memory_pipeline: Optional[MemoryPipeline] = None

    # ---- Hook 管理（事件驱动） ----

    def on(self, event: str, fn) -> None:
        """注册一个 hook 回调。

        Args:
            event: 事件名，如 "before_llm_call"。
            fn: 回调函数，接受 emit() 传入的 **kwargs。
        """
        self._hook_bus.on(event, fn)

    def _register_legacy_hooks(self):
        """将 self.hooks 的方法注册到 bus（向后兼容）。"""
        h = self.hooks
        self._hook_bus.on("before_llm_call",
            lambda **kw: h.before_llm_call(kw.get("messages"), kw.get("config")))
        self._hook_bus.on("after_llm_call",
            lambda **kw: h.after_llm_call(kw.get("response"), kw.get("messages")))
        self._hook_bus.on("before_tool_dispatch",
            lambda **kw: h.before_tool_dispatch(kw.get("name"), kw.get("args"), kw.get("ctx")))
        self._hook_bus.on("after_tool_dispatch",
            lambda **kw: h.after_tool_dispatch(kw.get("name"), kw.get("result"), kw.get("ctx")))
        self._hook_bus.on("before_response",
            lambda **kw: h.before_response(kw.get("reply"), kw.get("steps")))
        self._hook_bus.on("on_error",
            lambda **kw: h.on_error(kw.get("error")))

    # ---- Skill 管理（SKILL.md 生态） ----

    def list_skill_defs(self) -> List["SkillDefinition"]:
        """列出所有发现的 SKILL.md 技能定义。"""
        return self.skill_registry.list_skill_defs()

    def get_skill_def(self, name: str) -> Optional["SkillDefinition"]:
        """按名称获取 SKILL.md 技能定义。"""
        return self.skill_registry.get_skill_def(name)

    def match_skills(self, query: str, top_k: int = 3) -> List["SkillDefinition"]:
        """按用户意图匹配最相关的 SKILL.md 技能。"""
        return self.skill_registry.match_skills(query, top_k=top_k)

    def discover_skills(self, extra_dirs: Optional[List[str]] = None) -> int:
        """手动触发重新发现 SKILL.md 技能。"""
        return self.skill_registry.discover(extra_dirs=extra_dirs)

    # ---- Tool 管理 ----

    def register_tool(
        self,
        name: str,
        handler: Any,
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
    ) -> None:
        """注册一个自定义工具。

        Args:
            name: 工具名称（需唯一）。
            handler: 回调 (args: dict, ctx: dict) -> ToolResult。
            description: 工具描述（LLM 可见）。
            parameters: JSON Schema 参数定义。
        """
        self.tool_registry.register(name, handler, description, parameters)

    def unregister_tool(self, name: str) -> None:
        self.tool_registry.unregister(name)

    async def _handle_memory_turn(self, event: MemoryTurnEvent) -> None:
        await postprocess_turn(
            self,
            event.user_message,
            event.reply,
            event.all_steps_out,
            [event.step_count],
            event.session_id,
        )

    async def _ensure_memory_pipeline(self) -> MemoryPipeline:
        if self._memory_pipeline is None:
            self._memory_pipeline = MemoryPipeline(handler=self._handle_memory_turn)
            await self._memory_pipeline.start()
        return self._memory_pipeline

    async def enqueue_memory_turn(
        self,
        *,
        user_message: str,
        reply: str,
        all_steps_out: List[Dict[str, Any]],
        step_count: int,
        session_id: Optional[str],
    ) -> None:
        pipeline = await self._ensure_memory_pipeline()
        await pipeline.submit(
            MemoryTurnEvent(
                session_id=session_id,
                user_message=user_message,
                reply=reply,
                all_steps_out=all_steps_out,
                step_count=step_count,
            )
        )

    def list_tools(self) -> List[str]:
        return self.tool_registry.list_tools()

    # ---- SubAgent 管理 ----

    def create_sub_agent(
        self,
        name: str,
        namespace: Optional[str] = None,
    ) -> "Agent":
        """创建一个 SubAgent。

        SubAgent 继承当前 Agent 的所有端口,但拥有独立的 namespace。
        父 Agent 可通过 get_sub_agent() 获取子 Agent 并收集其结果。
        """
        sub_namespace = namespace or f"{self.namespace or 'agent'}:sub:{name}"
        sub = Agent(
            db=self.db,
            llm=self.llm,
            image_port=self.image_port,
            namespace=sub_namespace,
        )
        self._sub_agents[name] = sub
        return sub

    def get_sub_agent(self, name: str) -> Optional["Agent"]:
        return self._sub_agents.get(name)

    def remove_sub_agent(self, name: str) -> None:
        self._sub_agents.pop(name, None)

    def list_sub_agents(self) -> Dict[str, "Agent"]:
        return dict(self._sub_agents)

    # ---- 主循环（委托给 AgentExecutor） ----

    async def chat_stream_events(
        self,
        user_message: str,
        session_id: Optional[str],
        config: AgentConfig,
        images: Optional[List[str]] = None,
        channel_ctx: Optional[Dict[str, Any]] = None,
    ):
        """流式 Agent 对话。

        Args:
            user_message: 用户输入文本
            session_id: 会话 ID（None 表示不持久化）
            config: Agent 配置（base_url, sys_prompt, api_spec）
            images: 图片 Base64 列表
            channel_ctx: 渠道上下文（如 {"chat_id": ...}），供工具读取

        Yields:
            (event_type, event_data) 事件对
        """
        executor = AgentExecutor(agent=self)  # defined below
        async for ev in executor.run(
            user_message=user_message,
            session_id=session_id,
            config=config,
            images=images,
            channel_ctx=channel_ctx,
        ):
            yield ev



# ──────────────────────────────────────────
# AgentExecutor（原 engine/executor.py）
# ──────────────────────────────────────────

class AgentExecutor:
    """执行 Agent 决策循环。

    持有 Agent 实例引用，通过其端口完成 LLM 调用和工具分发。
    """

    def __init__(self, agent):
        self.agent = agent

    async def run(
        self,
        user_message: str,
        session_id: Optional[str],
        config: AgentConfig,
        images: Optional[List[str]] = None,
        channel_ctx: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
        """流式 Agent 对话。

        Args:
            user_message: 用户输入文本
            session_id: 会话 ID（None 表示不持久化）
            config: Agent 配置（base_url, sys_prompt, api_spec）
            images: 图片 Base64 列表
            channel_ctx: 渠道上下文（chat_id 等），透传给工具

        Yields:
            (event_type, event_data) 事件对
        """
        final_reply = ""
        last_text_stream = ""
        step_counter = [0]

        root_id = getattr(config, "root_id", None)

        # ---- 图片入库 ----
        stored_image_ids: List[str] = []
        if session_id and images:
            for img_b64 in images:
                img_id = await asyncio.to_thread(self.agent.db.add_session_image, session_id, img_b64)
                stored_image_ids.append(img_id)

        # ---- 消息入库 ----
        if session_id and user_message:
            await asyncio.to_thread(self.agent.db.add_agent_message, session_id, "user", user_message, type="text", root_id=root_id)

        # ---- 构建消息 ----
        _t_messages = time.time()
        messages = await asyncio.to_thread(
            build_agent_messages_with_history,
            session_id=session_id,
            sys_prompt=config.sys_prompt,
            user_message=user_message,
            db=self.agent.db,
            user_images=images,
            stored_image_ids=stored_image_ids or None,
            llm=self.agent.llm,
            namespace=self.agent.namespace,
            history_max_messages=getattr(config, "history_max_messages", 120),
            history_max_tokens=getattr(config, "history_max_tokens", 200_000),
            root_id=getattr(config, "root_id", None),
            supports_vision=self.agent.llm.supports_vision,
        )
        if getattr(config, "extra_system_messages", None):
            messages.extend(config.extra_system_messages)
        logger.info(f"[Agent] build_agent_messages_with_history 耗时={time.time()-_t_messages:.1f}s")

        # ---- 注入 SKILL.md 技能索引 ----
        _t_skill = time.time()
        skill_block = self.agent.skill_registry.build_skill_context()
        if skill_block:
            logger.info(f"[Agent] build_skill_context 耗时={time.time()-_t_skill:.1f}s")
        if skill_block:
            messages.append({
                "role": "system",
                "content": [{"type": "text", "text": skill_block}],
            })

        mcp_block = None
        if getattr(self.agent, "_mcp_manager", None) is not None:
            try:
                mcp_block = self.agent._mcp_manager.build_awareness_summary()
            except Exception:
                mcp_block = None
        if mcp_block:
            messages.append({
                "role": "system",
                "content": [{"type": "text", "text": mcp_block}],
            })

        logger.debug(f"[Agent] final messages count={len(messages)}")
        for idx, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", [])
            content_types = []
            for c in content:
                if isinstance(c, dict):
                    content_types.append(c.get("type", "unknown"))
            logger.debug(f"[Agent] msg[{idx}] role={role}, content_types={content_types}")

        all_step_results: Dict[int, Dict] = {}
        all_steps_out: List[Dict[str, Any]] = []

        yield ("session", {"session_id": session_id})

        # ---- Tool 上下文 ----
        def _yield_event(ev_type: str, ev_data: Dict[str, Any]) -> None:
            _pending_events.append((ev_type, ev_data))

        # ---- 决策循环 ----
        for _round in range(_MAX_ROUNDS):
            step_counter[0] += 1
            yield ("status", {"message": "思考中…"})

            _pending_events: List[Tuple[str, Dict[str, Any]]] = []

            # ---- P3：ReAct 边界 drain（后台事件注入；首轮跳过，避免连续 user 消息）----
            if _round > 0:
                try:
                    _drained = _drain_boundary_events(channel_ctx)
                    if _drained:
                        messages.append({
                            "role": "user",
                            "content": [{"type": "text", "text": _format_boundary_events(_drained)}],
                        })
                        logger.info("[Agent] boundary injected %d event(s)", len(_drained))
                except Exception:
                    logger.exception("[Agent] boundary drain failed")

            tool_context = {
                "db": self.agent.db,
                "llm": self.agent.llm,
                "image_port": self.agent.image_port,
                "config": config,
                "agent": self.agent,
                "session_id": session_id,
                "namespace": self.agent.namespace,
                "user_message": user_message,
                "user_images": images,
                "step_counter": step_counter,
                "all_steps_out": all_steps_out,
                "all_step_results": all_step_results,
                "yield_event": _yield_event,
                "delegate_depth": 0,
                "channel_ctx": channel_ctx or {},
            }

            # ---- 调用 LLM ----
            _t_schemas = time.time()
            tools_schemas = self.agent.tool_registry.get_available_schemas()
            logger.info(f"[Agent] get_available_schemas 耗时={time.time()-_t_schemas:.1f}s")
            try:
                for r in self.agent._hook_bus.emit("before_llm_call", messages=messages, config=config):
                    if r is not None:
                        messages = r
                _session_tag = session_id or "unknown"
                _channel = tool_context.get("channel", "unknown") if isinstance(tool_context, dict) else "unknown"
                _dump_path = Path(os.environ.get("INSTANCE_DIR", ".")) / "runtime" / f"llm_{_session_tag[:8]}.json"
                _dump_data = {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "session_id": session_id,
                    "channel": _channel,
                    "tools_schemas": tools_schemas,
                    "messages": messages,
                }
                _dump_path.parent.mkdir(parents=True, exist_ok=True)
                _dump_path.write_text(json.dumps(_dump_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                _t_llm = time.time()
                response = await asyncio.to_thread(self.agent.llm.chat, messages, temperature=0.1, tools=tools_schemas)
                logger.info(f"[Agent] LLM call 耗时={time.time()-_t_llm:.1f}s")
                for r in self.agent._hook_bus.emit("after_llm_call", response=response, messages=messages):
                    if r is not None:
                        response = r
            except Exception as e:
                self.agent._hook_bus.emit("on_error", error=e)
                logger.error(f"Agent LLM chat error: {e}")
                yield ("error", {"error": str(e)})
                return

            if not response:
                yield ("error", {"error": "LLM returned empty response"})
                return

            # ---- 兜底：content 中内嵌 tool_calls JSON（常见于 reasoning 模型降级） ----
            if response.content and not response.has_tool_calls:
                parsed_tool_calls = _try_extract_tool_calls_from_content(response.content)
                if parsed_tool_calls:
                    response.tool_calls = parsed_tool_calls
                    logger.info(f"[Agent] 从 content 中解析到 {len(parsed_tool_calls)} 个 tool_calls，走 tool_call 分支")
                    if response.content:
                        yield ("text_stream", {"content": response.content})
                    assistant_msg = {
                        "role": "assistant",
                        "content": response.content,
                        "reasoning_content": response.reasoning_content,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                                },
                            }
                            for tc in parsed_tool_calls
                        ],
                    }
                    messages.append(assistant_msg)
                    if session_id:
                        try:
                            await asyncio.to_thread(
                                self.agent.db.add_agent_message,
                                session_id, "assistant", json.dumps(assistant_msg, ensure_ascii=False),
                                reasoning_content=response.reasoning_content,
                                root_id=root_id,
                            )
                        except Exception as e:
                            logger.error(f"[Agent] save message error: {e}")
                    for tc in parsed_tool_calls:
                        yield ("tool_call", {
                            "name": tc.name,
                            "preview": _arg_preview(tc.arguments),
                            "id": tc.id,
                        })
                        _blocked = False
                        for _r in self.agent._hook_bus.emit("before_tool_dispatch", name=tc.name, args=tc.arguments, ctx=tool_context):
                            if _r is False:
                                result = ToolResult(ok=False, error=f"工具 [{tc.name}] 被 hook 拦截", signal="__continue__")
                                _blocked = True
                                break
                            elif isinstance(_r, dict):
                                tc.arguments = _r
                        if not _blocked:
                            result = await asyncio.to_thread(self.agent.tool_registry.dispatch, tc.name, tc.arguments, tool_context)
                        for _r in self.agent._hook_bus.emit("after_tool_dispatch", name=tc.name, result=result, ctx=tool_context):
                            if _r is not None:
                                result = _r
                        for ev in _pending_events:
                            yield ev
                        _pending_events.clear()
                        yield ("tool_result", {
                            "name": tc.name,
                            "ok": result.ok,
                            "summary": result.content_text[:200] if result.ok else (result.error or "")[:200],
                        })
                        if result.signal == "__ask__":
                            return
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result.content_text[:3000],
                        })
                    continue

            # ---- Text-only = final reply ----
            if response.content and not response.has_tool_calls:
                last_text_stream = response.content.strip()
                yield ("text_stream", {"content": response.content})
                reply = response.content.strip() or "已完成。"
                for r in self.agent._hook_bus.emit("before_response", reply=reply, steps=all_steps_out):
                    if r is not None:
                        reply = r

                if session_id:
                    try:
                        await asyncio.to_thread(self.agent.db.add_agent_message, session_id, "assistant", reply, root_id=root_id)
                    except Exception as e:
                        logger.error(f"[Agent] save message error: {e}")

                payload: Dict[str, Any] = {
                    "ok": True,
                    "reply": reply,
                    "session_id": session_id,
                    "steps": all_steps_out,
                }
                yield ("done", payload)

                # 后处理（不阻塞用户回复）
                asyncio.create_task(
                    self.agent.enqueue_memory_turn(
                        user_message=user_message,
                        reply=reply,
                        all_steps_out=all_steps_out,
                        step_count=step_counter[0],
                        session_id=session_id,
                    )
                )
                return

            # ---- Tool calls ----
            if response.has_tool_calls:
                assistant_msg = {
                    "role": "assistant",
                    "content": response.content,
                    "reasoning_content": response.reasoning_content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in response.tool_calls
                    ],
                }
                messages.append(assistant_msg)

                if response.content:
                    try:
                        yield ("text_stream", {"content": response.content})
                    except Exception:
                        pass

                if session_id:
                    try:
                        await asyncio.to_thread(
                            self.agent.db.add_agent_message,
                            session_id, "assistant", json.dumps(assistant_msg, ensure_ascii=False),
                            reasoning_content=response.reasoning_content,
                            root_id=root_id,
                        )
                    except Exception as e:
                        logger.error(f"[Agent] save message error: {e}")

                for tc in response.tool_calls:
                    yield ("tool_call", {
                        "name": tc.name,
                        "preview": _arg_preview(tc.arguments),
                        "id": tc.id,
                    })

                    _blocked = False
                    for _r in self.agent._hook_bus.emit("before_tool_dispatch", name=tc.name, args=tc.arguments, ctx=tool_context):
                        if _r is False:
                            result = ToolResult(ok=False, error=f"工具 [{tc.name}] 被 hook 拦截", signal="__continue__")
                            _blocked = True
                            break
                        elif isinstance(_r, dict):
                            tc.arguments = _r
                    if not _blocked:
                        result = await asyncio.to_thread(self.agent.tool_registry.dispatch, tc.name, tc.arguments, tool_context)

                    for _r in self.agent._hook_bus.emit("after_tool_dispatch", name=tc.name, result=result, ctx=tool_context):
                        if _r is not None:
                            result = _r

                    for ev in _pending_events:
                        yield ev
                    _pending_events.clear()

                    yield ("tool_result", {
                        "name": tc.name,
                        "ok": result.ok,
                        "summary": result.content_text[:200] if result.ok else (result.error or "")[:200],
                    })

                    if result.signal == "__ask__":
                        return

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result.content_text[:3000],
                    })

                continue

            # ---- Empty response ----
            yield ("error", {"error": "LLM returned empty response"})
            return

        # ---- Loops exhausted ----
        payload: Dict[str, Any] = {
            "ok": True,
            "reply": final_reply or last_text_stream or "已执行完成。",
            "session_id": session_id,
            "steps": all_steps_out,
        }
        yield ("done", payload)


def _try_extract_tool_calls_from_content(content: str) -> Optional[List["ToolCall"]]:
    """尝试从 LLM 返回的 content 文本中提取 tool_calls JSON。

    DeepSeek 等 reasoning 模型有时会在 content 中以 JSON 格式返回
    {"reasoning_content": "...", "tool_calls": [{...}]}，而非原生 tool_calls 字段。
    """
    if not content:
        return None
    cleaned = content.strip()
    # 尝试去掉外层可能的 markdown 代码块
    if cleaned.startswith("```"):
        end = cleaned.find("```", 3)
        if end > 0:
            cleaned = cleaned[3:end].strip()
    # 提取最外层的 JSON 对象
    brace_start = cleaned.find("{")
    brace_end_len = _find_matching_brace(cleaned, brace_start) if brace_start >= 0 else -1
    if brace_start < 0 or brace_end_len <= 0:
        return None
    json_str = cleaned[brace_start:brace_start + brace_end_len]
    try:
        obj = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return None
    tcs = obj.get("tool_calls") or obj.get("toolCalls") or obj.get("function_call")
    if not tcs:
        # 也可能是单个 function_call 格式
        fc = obj.get("function")
        if isinstance(fc, dict) and fc.get("name"):
            tcs = [fc]
    if not tcs:
        return None
    if not isinstance(tcs, list):
        tcs = [tcs]
    result = []
    for i, tc in enumerate(tcs):
        if not isinstance(tc, dict):
            continue
        tc_id = tc.get("id") or f"call_fallback_{i}"
        tc_name = tc.get("name") or ""
        if not tc_name:
            fc = tc.get("function") or {}
            tc_name = fc.get("name") if isinstance(fc, dict) else ""
        tc_args = tc.get("arguments") or {}
        if isinstance(tc.get("function"), dict):
            tc_args = tc["function"].get("arguments") or tc_args
        if isinstance(tc_args, str):
            try:
                tc_args = json.loads(tc_args)
            except (json.JSONDecodeError, ValueError):
                tc_args = {"_raw": tc_args}
        if not tc_name:
            continue
        result.append(ToolCall(id=tc_id, name=tc_name, arguments=tc_args))
    return result or None


def _find_matching_brace(text: str, start: int) -> int:
    """找到从 start 位置 '{' 开始的匹配 '}' 的长度。"""
    if start < 0 or start >= len(text) or text[start] != "{":
        return -1
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i - start + 1
    return -1


def _arg_preview(args: dict) -> str:
    """从工具参数中提取最重要的部分作为进度提示。"""
    if not args:
        return ""
    for key in ("query", "keyword", "question", "command", "path", "name", "skill", "package"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            v = val.strip()
            return v[:60] + ("..." if len(v) > 60 else "")
    for v in args.values():
        if isinstance(v, str) and v.strip():
            v = v.strip()
            return v[:60] + ("..." if len(v) > 60 else "")
    keys = list(args.keys())
    return ", ".join(keys[:3]) + ("..." if len(keys) > 3 else "")
