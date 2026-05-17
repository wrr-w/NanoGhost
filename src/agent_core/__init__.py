"""
agent-core: 独立的 LLM Agent 框架。

核心概念:
    Agent       - 对象化 Agent（多实例、命名空间隔离）
    Skill       - 可插拔能力单元
    DatabasePort - 持久化接口
    LLMPort     - LLM 调用接口
    HttpPort    - HTTP 调用接口
    ImagePort   - 图片存储接口

用法:
    from agent_core import Agent
    from agent_core.engine import AgentConfig

    agent = Agent(db=..., llm=..., http=..., image_port=..., namespace="my_app")
    for ev_type, ev_data in agent.chat_stream_events(
        "帮我建一个任务", session_id="...",
        config=AgentConfig(base_url="...", sys_prompt="..."),
    ):
        print(ev_type, ev_data)
"""

from .agent import Agent, _get_default_db, _default_db
from .engine import AgentConfig, build_agent_messages_with_history
from .interfaces import DatabasePort, LLMPort, HttpPort, ImagePort, LLMResponse
from .memory import (
    record_successful_flow,
    retrieve_similar_flows,
    record_memory_feedback,
    list_flows,
    update_graph_from_steps,
    suggest_next_nodes,
)
from .skill import Skill, SkillResult, SkillRegistry, SkillDefinition, discover_skills, load_skill_from_dir
from .tool import ToolCall, ToolDefinition, ToolResult, ToolRegistry, register_builtins
from .utils import extract_json_from_llm_response, image2base64

__all__ = [
    # 核心类
    "Agent",
    "AgentConfig",
    # 端口
    "DatabasePort",
    "LLMPort",
    "HttpPort",
    "ImagePort",
    "LLMResponse",
    # Skill（旧 ABC 体系）
    "Skill",
    "SkillResult",
    # Skill（新 SKILL.md 生态）
    "SkillRegistry",
    "SkillDefinition",
    "discover_skills",
    "load_skill_from_dir",
    # Tool 系统
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "ToolRegistry",
    "register_builtins",
    # 引擎工具
    "build_agent_messages_with_history",
    # 记忆系统
    "record_successful_flow",
    "retrieve_similar_flows",
    "record_memory_feedback",
    "list_flows",
    "update_graph_from_steps",
    "suggest_next_nodes",
    # 通用工具
    "extract_json_from_llm_response",
    "image2base64",
    # 底层（极少使用）
    "_get_default_db",
    "_default_db",
]
