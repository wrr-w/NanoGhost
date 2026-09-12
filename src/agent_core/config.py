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


# ── 运行模式 ──
#
# AGENT_MODE 决定这个进程跑成什么（cli 交互 / feishu 长连接）。它必须是**单一
# 真相**：网关孵 worker、控制台启停通道、手工 `NanoGhost.exe -I cc`，看到的都得
# 是同一个答案。答案在实例的 channel_directory.json 里（网关也只读它）。
#
# 历史写法是把它写进实例 .env。那是错的，而且错得很安静：run.py 加载 .env 用的是
# load_dotenv(override=True)，.env 里的值会把网关传进来的 AGENT_MODE **顶掉** ——
# 于是控制台里"启用飞书"点成功了、worker 起来却在跑 CLI，几秒钟就退出，30 秒后
# 体检再孵一遍，无限循环。所以 .env 里的 AGENT_MODE 现在被忽略。

AGENT_MODE_CLI = "cli"
AGENT_MODE_FEISHU = "feishu"

# 唯一有 worker 的通道。和 gateway_server.py 的 CHANNEL_REGISTRY 里带 worker_key
# 的那几项是同一件事 —— 那边是网关侧的定义，这份是"从文件反推模式"用的，将来
# 加第二个 worker 通道时要一起改。
WORKER_CHANNEL = AGENT_MODE_FEISHU


def channel_enabled(instance_dir: "str | os.PathLike | None", channel: str) -> bool:
    """实例的 channel_directory.json 里某个通道是否启用。读不出来 = 没启用。"""
    inst = str(instance_dir or "").strip()
    if not inst:
        return False
    path = Path(os.path.abspath(os.path.expanduser(inst))) / "channel_directory.json"
    channels = _read_json(path).get("channels")
    if not isinstance(channels, dict):
        return False
    cfg = channels.get(channel)
    return bool(cfg.get("enabled")) if isinstance(cfg, dict) else False


def resolve_agent_mode(instance_dir: "str | os.PathLike | None",
                       *, explicit: str = "") -> "tuple[str, str]":
    """返回 (模式, 来源)。来源用于诊断输出：env / channel / default。

    explicit 是**调用方显式指定的**模式（网关孵 worker 时传 AGENT_MODE=feishu）。
    它优先，因为"我是哪一个 worker"只有孵它的那个人知道 —— 光看 channel_directory
    .json 只能得出"飞书开着"，分不出这个进程是该当飞书 worker 还是该当网关。
    传进来的 explicit 会盖过 .env，但 .env 本身不再参与判断。
    """
    m = _clean_env_value(explicit).lower()
    if m:
        return m, "env"
    if channel_enabled(instance_dir, WORKER_CHANNEL):
        return WORKER_CHANNEL, "channel"
    return AGENT_MODE_CLI, "default"


# ── 单次对话配置 ──

@dataclass
class AgentConfig:
    """Agent 配置"""
    base_url: str
    sys_prompt: str
    api_spec: Dict[str, Any] = field(default_factory=dict)
    extra_system_messages: List[Dict[str, Any]] = field(default_factory=list)
    """每轮额外注入的 system messages，例如 daily memory。"""
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
