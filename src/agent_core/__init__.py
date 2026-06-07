"""
agent-core: 独立的 LLM Agent 框架。

核心概念:
    Agent         - 对象化 Agent（多实例、命名空间隔离）
    Skill         - 可插拔能力单元
    DatabasePort  - 持久化接口
    LLMPort       - LLM 调用接口
    ImagePort     - 图片存储接口

内置适配器:
    from agent_core.adapters import SqliteDatabase, OpenAILLM, SqliteImagePort

用法:
    from agent_core import Agent
    from agent_core.adapters import SqliteDatabase, OpenAILLM, SqliteImagePort
    from agent_core.config import AgentConfig

    agent = Agent(
        db=SqliteDatabase(),
        llm=OpenAILLM(),
        image_port=SqliteImagePort(),
        namespace="my_app",
    )
    for ev_type, ev_data in agent.chat_stream_events(
        "帮我建一个任务", session_id="...",
        config=AgentConfig(base_url="...", sys_prompt="..."),
    ):
        print(ev_type, ev_data)
"""

from .engine.agent import Agent, _get_default_db, _default_db
from .config import AgentConfig, load_instance_config, InstanceConfig
from .engine.messages import build_agent_messages_with_history
from .interfaces import DatabasePort, LLMPort, ImagePort, LLMResponse
from .memory import (
    record_successful_flow,
    retrieve_similar_flows,
    record_memory_feedback,
    list_flows,
    update_graph_ml,
)
from .skill import SkillRegistry, SkillDefinition, discover_skills, load_skill_from_dir
from .hooks import AgentHooks, HookBus
from .tool import ToolCall, ToolDefinition, ToolResult, ToolRegistry, register_builtins
from .utils import load_yaml_subset, pid_exists, terminate_pid

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentHooks",
    "HookBus",
    "InstanceConfig",
    "load_instance_config",
    "DatabasePort",
    "LLMPort",
    "ImagePort",
    "LLMResponse",
    "SkillRegistry",
    "SkillDefinition",
    "discover_skills",
    "load_skill_from_dir",
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "ToolRegistry",
    "register_builtins",
    "build_agent_messages_with_history",
    "record_successful_flow",
    "retrieve_similar_flows",
    "record_memory_feedback",
    "list_flows",
    "update_graph_ml",
    "load_yaml_subset",
    "pid_exists",
    "terminate_pid",
    "_get_default_db",
    "_default_db",
]
