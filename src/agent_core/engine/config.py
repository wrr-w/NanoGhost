from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentConfig:
    """Agent 配置"""
    base_url: str
    sys_prompt: str
    api_spec: Dict[str, Any] = field(default_factory=dict)
    skill_extra_dirs: Optional[List[str]] = None
    """额外搜索 SKILL.md 的目录。"""
    shell_timeout: int = 120
    """shell 命令默认超时秒数。"""
    shell_cwd: Optional[str] = None
    """shell 命令工作目录（默认当前目录）。"""
    verbose: bool = True
    """是否输出详细事件（tool_call/tool_result/subagent 等）。
       False 时只输出 text_stream、done、error 等用户可见事件。"""
