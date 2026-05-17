"""
Agent 主类：对象化、多实例、Skill 可插拔、SubAgent 可派生。

每个 Agent 实例拥有：
- 独立的端口注入（db, llm, http, image_port）
- 独立的 namespace（记忆隔离）
- 独立的 SkillRegistry
- 工具注册表（ToolRegistry，Hermes 风格 function calling）
- 可创建 SubAgent（继承端口，独立 namespace）

用法:
    from agent_core import Agent
    from agent_core.engine.config import AgentConfig

    agent = Agent(db=my_db, llm=my_llm, http=my_http, namespace="my_app")
    for ev_type, ev_data in agent.chat_stream_events(
        user_message="帮我建一个任务",
        session_id="...",
        config=AgentConfig(base_url="...", sys_prompt="...", api_spec={}),
    ):
        print(ev_type, ev_data)
"""

import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent_core.engine.config import AgentConfig
from agent_core.engine.messages import build_agent_messages_with_history
from agent_core.interfaces import DatabasePort, ImagePort, LLMPort, HttpPort, LLMResponse
from agent_core.memory.cards import record_successful_flow
from agent_core.memory.graph import update_graph_from_steps
from agent_core.skill import SkillRegistry, Skill, SkillDefinition
from agent_core.tool import ToolRegistry, ToolCall, ToolResult, register_builtins

logger = logging.getLogger("agent_core")

_MAX_ROUNDS = 20

# 模块级默认 db（兼容简单场景）
_default_db: Optional[DatabasePort] = None


def _get_default_db() -> DatabasePort:
    global _default_db
    if _default_db is None:
        raise RuntimeError("No default DatabasePort configured. Pass db explicitly or set agent_core.agent._default_db.")
    return _default_db


class Agent:
    """Agent 主类。每个实例独立管理端口、记忆域、Skill、工具和 SubAgent。"""

    def __init__(
        self,
        db: DatabasePort,
        llm: LLMPort,
        http: HttpPort,
        image_port: Optional[ImagePort] = None,
        namespace: Optional[str] = None,
        auto_discover_skills: bool = True,
        skill_extra_dirs: Optional[List[str]] = None,
        auto_register_tools: bool = True,
    ):
        self.db = db
        self.llm = llm
        self.http = http
        self.image_port = image_port
        self.namespace = namespace

        # Skill 系统（兼容旧 ABC + 新 SKILL.md 生态）
        self.skill_registry = SkillRegistry()

        # 自动发现 SKILL.md 技能
        if auto_discover_skills:
            self.skill_registry.discover(extra_dirs=skill_extra_dirs)

        # Tool 注册表（Hermes 风格 function calling）
        self.tool_registry = ToolRegistry()
        if auto_register_tools:
            register_builtins(self.tool_registry)

        # SubAgent 管理
        self._sub_agents: Dict[str, "Agent"] = {}

    # ---- Skill 管理（旧 ABC 体系） ----

    def register_skill(self, skill: Skill) -> None:
        """注册一个 Python ABC Skill。"""
        self.skill_registry.register(skill)

    def unregister_skill(self, name: str) -> None:
        self.skill_registry.unregister(name)

    def list_skills(self) -> List[Skill]:
        return self.skill_registry.list()

    # ---- Skill 管理（新 SKILL.md 生态） ----

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
            http=self.http,
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

    # ---- 主循环 ----

    def chat_stream_events(
        self,
        user_message: str,
        session_id: Optional[str],
        config: AgentConfig,
        images: Optional[List[str]] = None,
    ) -> Iterable[Tuple[str, Dict[str, Any]]]:
        """流式 Agent 对话。

        Args:
            user_message: 用户输入文本
            session_id: 会话 ID（None 表示不持久化）
            config: Agent 配置（base_url, sys_prompt, api_spec）
            images: 图片 Base64 列表

        Yields:
            (event_type, event_data) 事件对
        """
        final_reply = ""
        step_counter = [0]  # mutable for tool handlers

        # ---- 图片入库 ----
        stored_image_ids = []
        if session_id and images and self.image_port:
            try:
                for img_base64 in images:
                    img_id = self.image_port.add_image(img_base64)
                    stored_image_ids.append(img_id)
                    self.db.add_agent_message(session_id, "user", img_id, type="image")
            except Exception as e:
                logger.error(f"[Agent] save user message error: {e}")

        if stored_image_ids:
            yield ("images_stored", {"image_ids": stored_image_ids})

        # ---- 文本入库 ----
        if session_id and user_message:
            self.db.add_agent_message(session_id, "user", user_message, type="text")

        # ---- 构建消息 ----
        messages = build_agent_messages_with_history(
            session_id=session_id,
            sys_prompt=config.sys_prompt,
            user_message=user_message,
            db=self.db,
            user_images=images,
            image_urls=stored_image_ids,
            llm=self.llm,
            namespace=self.namespace,
        )

        # ---- 注入 SKILL.md 技能索引（轻量列表，按需加载） ----
        # 以 user message 形式注入（而非 system prompt），不破坏 prompt caching
        skill_block = self.skill_registry.build_skill_context()
        if skill_block:
            messages.insert(1, {
                "role": "user",
                "content": [{"type": "text", "text": skill_block}],
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

        # ---- Tool 上下文（传给内置工具 handlers） ----
        def _yield_event(ev_type: str, ev_data: Dict[str, Any]) -> None:
            """Yield an event from within a tool handler."""
            # Store event for outer loop to yield
            _pending_events.append((ev_type, ev_data))

        # ---- 决策循环 ----
        for _round in range(_MAX_ROUNDS):
            yield ("status", {"message": "思考中…"})

            _pending_events: List[Tuple[str, Dict[str, Any]]] = []

            tool_context = {
                "db": self.db,
                "llm": self.llm,
                "http": self.http,
                "image_port": self.image_port,
                "config": config,
                "agent": self,
                "session_id": session_id,
                "namespace": self.namespace,
                "user_message": user_message,
                "step_counter": step_counter,
                "all_steps_out": all_steps_out,
                "all_step_results": all_step_results,
                "yield_event": _yield_event,
                "delegate_depth": 0,
            }

            # ---- 调用 LLM（始终传递 tool schemas） ----
            tools_schemas = self.tool_registry.get_available_schemas()
            try:
                response = self.llm.chat(messages, temperature=0.1, tools=tools_schemas)
            except Exception as e:
                logger.error(f"Agent LLM chat error: {e}")
                yield ("error", {"error": str(e)})
                return

            if not response:
                yield ("error", {"error": "LLM returned empty response"})
                return

            # ---- Text-only = final reply ----
            if response.content and not response.has_tool_calls:
                yield ("text_stream", {"content": response.content})
                reply = response.content.strip() or "已完成。"

                if session_id:
                    try:
                        self.db.add_agent_message(session_id, "assistant", reply)
                    except Exception as e:
                        logger.error(f"[Agent] save message error: {e}")

                flow_hash = None
                if all_steps_out:
                    try:
                        flow_hash = record_successful_flow(
                            user_message, all_steps_out, step_counter[0],
                            db=self.db, llm=self.llm, namespace=self.namespace,
                        )
                        update_graph_from_steps(all_steps_out, approved=False, db=self.db)
                    except Exception as e:
                        logger.error(f"[AgentMemory] record error: {e}")

                payload: Dict[str, Any] = {
                    "ok": True,
                    "reply": reply,
                    "session_id": session_id,
                    "steps": all_steps_out,
                }
                if flow_hash:
                    payload["flow_hash"] = flow_hash
                yield ("done", payload)
                return

            # ---- Tool calls ----
            if response.has_tool_calls:
                assistant_msg = {
                    "role": "assistant",
                    "content": response.content,
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
                        self.db.add_agent_message(
                            session_id, "assistant", json.dumps(assistant_msg, ensure_ascii=False),
                        )
                    except Exception as e:
                        logger.error(f"[Agent] save message error: {e}")

                for tc in response.tool_calls:
                    if config.verbose:
                        yield ("tool_call", {
                            "name": tc.name,
                            "arguments": tc.arguments,
                            "id": tc.id,
                        })

                    result = self.tool_registry.dispatch(tc.name, tc.arguments, tool_context)

                    for ev in _pending_events:
                        yield ev
                    _pending_events.clear()

                    if config.verbose:
                        yield ("tool_result", {
                            "name": tc.name,
                            "ok": result.ok,
                            "signal": result.signal,
                            "summary": result.content_text[:200],
                            "id": tc.id,
                        })

                    if result.signal == "__ask__":
                        return

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result.content_text[:3000],
                    })

                continue

            # ---- Empty response (shouldn't reach here) ----
            yield ("error", {"error": "LLM returned empty response"})
            return

        # ---- Loops exhausted ----
        payload: Dict[str, Any] = {
            "ok": True,
            "reply": final_reply or "已执行完成。",
            "session_id": session_id,
            "steps": all_steps_out,
        }
        yield ("done", payload)
