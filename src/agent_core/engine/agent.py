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
from typing import Any, Dict, List, Optional

from agent_core.config import AgentConfig
from agent_core.hooks import AgentHooks, HookBus
from agent_core.interfaces import DatabasePort, ImagePort, LLMPort
from agent_core.skill import SkillRegistry, SkillDefinition
from agent_core.tool import ToolRegistry, register_builtins

logger = logging.getLogger("agent_core")

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
                from agent_core.mcp import MCPManager
                from agent_core.infra.config_loader import load_instance_config

                inst_cfg = load_instance_config()
                cooldown = inst_cfg.extra.get("mcp_cooldown_seconds", 60)
                fail_threshold = inst_cfg.extra.get("mcp_fail_threshold", 3)
                probe_ttl = inst_cfg.extra.get("mcp_probe_ttl_seconds", 60)

                self._mcp_manager = MCPManager(
                    cooldown_seconds=cooldown,
                    fail_threshold=fail_threshold,
                    probe_ttl_seconds=probe_ttl,
                )
                self._mcp_manager.attach_tool_registry(self.tool_registry)
                import threading
                threading.Thread(target=self._mcp_manager.refresh_all, daemon=True).start()
                self._mcp_manager.start_poller()
            except Exception:
                self._mcp_manager = None

        # SubAgent 管理
        self._sub_agents: Dict[str, "Agent"] = {}

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
    ):
        """流式 Agent 对话。

        Args:
            user_message: 用户输入文本
            session_id: 会话 ID（None 表示不持久化）
            config: Agent 配置（base_url, sys_prompt, api_spec）
            images: 图片 Base64 列表

        Yields:
            (event_type, event_data) 事件对
        """
        from .executor import AgentExecutor
        executor = AgentExecutor(agent=self)
        async for ev in executor.run(
            user_message=user_message,
            session_id=session_id,
            config=config,
            images=images,
        ):
            yield ev
