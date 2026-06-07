import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_core.utils import load_yaml_subset


def _clean_env_value(v: Optional[str]) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if len(s) >= 2 and ((s[0] == s[-1] == "`") or (s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        s = s[1:-1].strip()
    return s


def _default_global_config_path() -> Path:
    home = Path.home()
    return home / ".nanoghost" / "config.yaml"


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


# ── 单次对话配置 ──

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
    history_max_messages: int = 200
    """单次对话保留的最大历史消息条数。"""
    history_max_tokens: int = 200_000
    """单次对话历史消息的最大估算 Token 数，超出则从旧消息截断。"""
    root_id: Optional[str] = None
    """话题根消息 ID。非空时，历史消息只加载同 root_id 的消息。"""


# ── 实例级配置 ──

@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = "gpt-4o"
    embed_model: str = "text-embedding-3-small"


@dataclass
class FeishuConfig:
    app_id: str = ""
    app_secret: str = ""
    verbose: bool = False


@dataclass
class SkillConfig:
    skills_dir: str = ""
    enabled_only: List[str] = field(default_factory=list)


@dataclass
class ChannelConfig:
    enabled: Dict[str, bool] = field(default_factory=lambda: {"cli": True, "feishu": False})


@dataclass
class InstanceConfig:
    instance_dir: Path = field(default_factory=Path)
    namespace: str = "agent"
    db_path: str = ""
    workdir: str = ""
    prompts_dir: str = ""
    base_url: str = "http://127.0.0.1:8000"

    llm: LLMConfig = field(default_factory=LLMConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    skill: SkillConfig = field(default_factory=SkillConfig)
    channel: ChannelConfig = field(default_factory=ChannelConfig)

    mcp_enabled_only: List[str] = field(default_factory=list)

    history_max_messages: int = 200
    history_max_tokens: int = 200_000

    extra: Dict[str, Any] = field(default_factory=dict)


def load_instance_config(instance_dir: Optional[str] = None) -> InstanceConfig:
    instance_dir_str = instance_dir or os.getenv("INSTANCE_DIR") or ""
    instance_dir_str = _clean_env_value(instance_dir_str)

    cfg = InstanceConfig()

    if instance_dir_str:
        inst_path = Path(os.path.abspath(os.path.expanduser(instance_dir_str)))
        cfg.instance_dir = inst_path
        cfg.namespace = os.path.basename(instance_dir_str.rstrip("\\/")) or "agent"
        cfg.db_path = os.path.join(str(inst_path), "data", "agent_data.db")
        cfg.workdir = os.path.join(str(inst_path), "work")
        cfg.prompts_dir = os.path.join(str(inst_path), "prompts")
        skills_dir = os.path.join(str(inst_path), "skills")
        if os.path.isdir(skills_dir):
            cfg.skill.skills_dir = skills_dir

        inst_yaml = load_yaml_subset(inst_path / "config.yaml")
        _apply_yaml_overrides(cfg, inst_yaml, str(inst_path))

        channel_json = _read_json(inst_path / "channel_directory.json")
        if channel_json:
            ch = channel_json.get("channels")
            if isinstance(ch, dict):
                for k, v in ch.items():
                    if isinstance(v, dict):
                        cfg.channel.enabled[k] = bool(v.get("enabled", False))

    global_yaml = load_yaml_subset(_default_global_config_path())
    if global_yaml:
        _apply_global_mcp(cfg, global_yaml)

    cfg.llm.api_key = os.getenv("LLM_API_KEY") or cfg.llm.api_key
    cfg.llm.base_url = os.getenv("LLM_BASE_URL") or cfg.llm.base_url
    cfg.llm.model = os.getenv("LLM_MODEL") or cfg.llm.model
    cfg.llm.embed_model = os.getenv("EMBED_MODEL") or cfg.llm.embed_model

    cfg.feishu.app_id = (os.getenv("FEISHU_APP_ID") or cfg.feishu.app_id)
    cfg.feishu.app_secret = (os.getenv("FEISHU_APP_SECRET") or cfg.feishu.app_secret)
    cfg.feishu.verbose = os.getenv("FEISHU_VERBOSE", "").lower() in ("1", "true", "yes")

    cfg.base_url = os.getenv("AGENT_BASE_URL", cfg.base_url).rstrip("/")

    if not cfg.skill.skills_dir:
        cfg.skill.skills_dir = os.getenv("AGENTS_SKILLS_DIR", os.path.expanduser("~/.agents/skills"))

    if os.getenv("AGENT_DB_PATH"):
        cfg.db_path = os.getenv("AGENT_DB_PATH")
    if os.getenv("AGENT_WORKDIR"):
        cfg.workdir = os.getenv("AGENT_WORKDIR")
    if os.getenv("AGENT_PROMPTS_DIR"):
        cfg.prompts_dir = os.getenv("AGENT_PROMPTS_DIR")
    if os.getenv("AGENT_NAMESPACE"):
        cfg.namespace = os.getenv("AGENT_NAMESPACE")

    return cfg


def _apply_yaml_overrides(cfg: InstanceConfig, yaml_data: Dict[str, Any], instance_dir_str: str) -> None:
    if not isinstance(yaml_data, dict):
        return

    mcp = yaml_data.get("mcp")
    if isinstance(mcp, dict):
        enabled = mcp.get("enabled_only")
        if isinstance(enabled, list):
            cfg.mcp_enabled_only = [str(x).strip() for x in enabled if str(x).strip()]

    skills = yaml_data.get("skills")
    if isinstance(skills, dict):
        enabled = skills.get("enabled_only")
        if isinstance(enabled, list):
            cfg.skill.enabled_only = [str(x).strip() for x in enabled if str(x).strip()]

    ch = yaml_data.get("channels")
    if isinstance(ch, dict):
        for k, v in ch.items():
            if isinstance(v, dict) and "enabled" in v:
                cfg.channel.enabled[k] = bool(v["enabled"])

    mcp_cfg = yaml_data.get("mcp_server_config")
    if isinstance(mcp_cfg, dict):
        cfg.extra["mcp_cooldown_seconds"] = int(mcp_cfg.get("cooldown_seconds", 60))
        cfg.extra["mcp_fail_threshold"] = int(mcp_cfg.get("fail_threshold", 3))
        cfg.extra["mcp_probe_ttl_seconds"] = int(mcp_cfg.get("probe_ttl_seconds", 60))

    history_cfg = yaml_data.get("history")
    if isinstance(history_cfg, dict):
        if "max_messages" in history_cfg:
            cfg.history_max_messages = int(history_cfg["max_messages"])
        if "max_tokens" in history_cfg:
            cfg.history_max_tokens = int(history_cfg["max_tokens"])


def _apply_global_mcp(cfg: InstanceConfig, yaml_data: Dict[str, Any]) -> None:
    cfg.extra["global_mcp_registry"] = yaml_data.get("mcp_servers", {})


def get_global_mcp_registry() -> Dict[str, Any]:
    yaml_data = load_yaml_subset(_default_global_config_path())
    reg = yaml_data.get("mcp_servers") if isinstance(yaml_data, dict) else {}
    return reg if isinstance(reg, dict) else {}
