"""
agent-core 独立启动入口。

用法:
    # CLI 交互模式（默认）
    python run.py
    python run.py "帮我创建一个任务"                    # 单次对话
    python run.py "帮我创建一个任务" --skill lark-calendar  # 加载技能后执行

    # 技能管理
    python run.py --list-skills                        # 列出可用技能
    python run.py --skill lark-calendar                # 查看技能内容

    # 飞书模式（指定实例；该实例的 channel_directory.json 里 feishu.enabled 为 true）
    python run.py -I <实例目录>
    # 想在开了飞书的实例上跑一次交互终端，就显式覆盖：
    set AGENT_MODE=cli
    python run.py -I <实例目录>

跑成什么模式由实例的 channel_directory.json 决定（网关只读它）。带 -I 时环境变量
AGENT_MODE 是"调用方显式指定"，优先级最高，但实例 .env 里的 AGENT_MODE 会被忽略 ——
见 src/agent_core/config.py 的 resolve_agent_mode。
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from dotenv import load_dotenv
from agent_core.config import resolve_agent_mode
from agent_core.memory.files import read_long_term_memory_block
from agent_core.setup_wizard import ensure_llm_configured
from agent_core.update import auto_check_and_notify


def _normalize_llm_env():
    for primary, *fallbacks in [
        ("LLM_API_KEY", "OPENAI_API_KEY", "API_KEY"),
        ("LLM_BASE_URL", "OPENAI_BASE_URL", "BASE_URL"),
        ("LLM_MODEL", "OPENAI_MODEL", "MODEL_NAME"),
    ]:
        if not (os.getenv(primary) or "").strip():
            for fb in fallbacks:
                v = (os.getenv(fb) or "").strip()
                if v:
                    os.environ[primary] = v
                    break

def _clean_env_value(v: str) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if len(s) >= 2 and ((s[0] == s[-1] == "`") or (s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        s = s[1:-1].strip()
    return s


def _preparse_instance_dir(argv: List[str]) -> str:
    inst = os.getenv("INSTANCE_DIR")
    if inst:
        return inst
    for i, a in enumerate(argv or []):
        if a in ("--instance-dir", "-I") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--instance-dir="):
            return a.split("=", 1)[1]
    return ""


def _bootstrap_instance(argv: List[str]) -> None:
    instance_dir = _clean_env_value(_preparse_instance_dir(argv))

    # 在**加载任何 .env 之前**就把调用方指定的模式记下来。网关孵 worker 时是靠
    # start_worker(env_overrides={"AGENT_MODE": wk}) 把意图传进来的，而下面那句
    # load_dotenv(override=True) 会把 .env 里的 AGENT_MODE 顶到这个值之上 —— 先
    # 记后加载，才拿得到调用方的原话。
    explicit_mode = _clean_env_value(os.getenv("AGENT_MODE"))

    global_env = os.path.join(os.path.expanduser("~"), ".nanoghost", ".env")
    if os.path.isfile(global_env):
        load_dotenv(dotenv_path=global_env, override=False)
    _normalize_llm_env()

    if not instance_dir:
        # 没有实例目录就没法反推模式，调用方传的（或没传）就是最终答案
        if explicit_mode:
            os.environ["AGENT_MODE"] = explicit_mode
        return

    instance_dir = os.path.abspath(os.path.expanduser(instance_dir))
    os.environ["INSTANCE_DIR"] = instance_dir

    os.makedirs(os.path.join(instance_dir, "data"), exist_ok=True)
    os.makedirs(os.path.join(instance_dir, "work"), exist_ok=True)
    os.makedirs(os.path.join(instance_dir, "skills"), exist_ok=True)

    os.environ.setdefault("AGENT_DB_PATH", os.path.join(instance_dir, "data", "agent_data.db"))
    os.environ.setdefault("AGENT_PROMPTS_DIR", os.path.join(instance_dir, "prompts"))
    os.environ.setdefault("AGENT_WORKDIR", os.path.join(instance_dir, "work"))
    os.environ.setdefault("AGENT_NAMESPACE", os.path.basename(instance_dir.rstrip("\\/")) or "agent")

    dotenv_path = os.path.join(instance_dir, ".env")
    if os.path.isfile(dotenv_path):
        load_dotenv(dotenv_path=dotenv_path, override=True)
        _normalize_llm_env()

    # 运行模式**不归 .env 管** —— 唯一开关是实例的 channel_directory.json（网关也只
    # 读它）。.env 里如果还留着 AGENT_MODE，上面那句 override=True 正好会把它读进来，
    # 所以这里必须重新裁一次，且必须在 .env 之后。见 agent_core/config.py 的说明。
    mode, mode_source = resolve_agent_mode(instance_dir, explicit=explicit_mode)
    os.environ["AGENT_MODE"] = mode
    # 让 `nanoghost diag` 能把"为什么是这个模式"一并打出来 —— 这个模式一旦错了，
    # 症状是"控制台说启用成功、进程却在秒退"，光看 AGENT_MODE 一眼看不出所以然
    os.environ["AGENT_MODE_SOURCE"] = mode_source


_bootstrap_instance(sys.argv)

_inst_dir = _clean_env_value(os.getenv("INSTANCE_DIR"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
)
if _inst_dir:
    _log_dir = os.path.join(_inst_dir, "runtime")
    os.makedirs(_log_dir, exist_ok=True)
    _log_path = os.path.join(_log_dir, "nanoghost.log")
    _fh = logging.FileHandler(_log_path, encoding="utf-8")
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
    ))
    logging.getLogger().addHandler(_fh)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in logging.getLogger().handlers):
        _sh = logging.StreamHandler()
        _sh.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
        ))
        logging.getLogger().addHandler(_sh)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("agent_core")

# 适配器已提取至 agent_core.adapters
from agent_core import Agent, AgentConfig
from agent_core.adapters import SqliteDatabase, OpenAILLM, SqliteImagePort
from agent_core.tool import ToolCall
from agent_core.config import load_instance_config

# ---------------------------------------------------------------------------
# ANSI colors / CLI formatter
# ---------------------------------------------------------------------------

class Style:
    """ANSI escape sequences for terminal coloring."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"

    # Foreground
    GRAY = "\033[90m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"

    # Background
    BG_GRAY = "\033[100m"
    BG_BLUE = "\033[44m"

    @classmethod
    def ok(cls, text: str) -> str:
        return f"{cls.GREEN}{text}{cls.RESET}"

    @classmethod
    def fail(cls, text: str) -> str:
        return f"{cls.RED}{text}{cls.RESET}"

    @classmethod
    def dim(cls, text: str) -> str:
        return f"{cls.DIM}{text}{cls.RESET}"

    @classmethod
    def bold(cls, text: str) -> str:
        return f"{cls.BOLD}{text}{cls.RESET}"

    @classmethod
    def tag(cls, label: str, text: str, color: str = "") -> str:
        """Format as [label] text with color."""
        label_styled = f"{cls.BOLD}{color}{label}{cls.RESET}" if color else f"{cls.BOLD}{label}{cls.RESET}"
        return f"{label_styled} {text}"


def _fmt_json(args: dict) -> str:
    """Compact one-line JSON for display."""
    return json.dumps(args, ensure_ascii=False, separators=(",", ": "))


def _fmt_event(ev_type: str, ev_data: dict) -> str:
    """Format a single event line for CLI display. Return empty string to skip."""
    if ev_type == "text_stream":
        return ev_data.get("content", "")

    if ev_type in ("status",):
        return ""

    if ev_type == "done":
        return ""

    if ev_type == "skill_loaded":
        name = ev_data.get("name", "")
        return f"\n{Style.tag('SKILL', name, Style.BLUE)}\n"

    if ev_type == "error":
        err = ev_data.get("error", "")
        return f"\n{Style.fail(f'Error: {err}')}\n"

    if ev_type == "tool_call":
        name = ev_data.get("name", "")
        args = ev_data.get("arguments", {})
        args_str = _fmt_json(args)
        return f"\n  {Style.CYAN}┌─ {Style.bold(name)}{Style.RESET} {Style.dim(args_str)}"

    if ev_type == "tool_result":
        ok = ev_data.get("ok", True)
        sig = ev_data.get("signal", "")
        summary = ev_data.get("summary", "").strip()
        icon = Style.ok("└─ OK") if ok else Style.fail("└─ FAIL")
        meta = ""
        if sig and sig != "__continue__":
            meta = f" {Style.dim(f'[{sig}]')}"
        body = f" {Style.dim(summary[:120])}" if summary else ""
        return f"  {icon}{body}{meta}"

    if ev_type == "subagent_start":
        stype = ev_data.get("type", "")
        desc = ev_data.get("description", "")
        return f"\n  {Style.MAGENTA}┌─ SubAgent[{stype}]{Style.RESET} {Style.dim(desc)}"

    if ev_type == "subagent_text":
        return ev_data.get("content", "")

    if ev_type == "subagent_result":
        reply = ev_data.get("reply", "").strip()
        return f"  {Style.MAGENTA}└─ Result:{Style.RESET} {Style.dim(reply[:200])}"

    if ev_type == "step_start":
        s = ev_data.get("step", "")
        m = ev_data.get("method", "")
        p = ev_data.get("path", "")
        return f"\n  {Style.YELLOW}→ Step {s}: {m} {p}"

    if ev_type == "step_done":
        ok = ev_data.get("ok", False)
        label = Style.ok("✓") if ok else Style.fail("✗")
        return f"  {label} {ev_data.get('path', '')}"

    if ev_type == "ask_user":
        return f"\n  {Style.BOLD}Question:{Style.RESET} {ev_data}\n"

    return ""


# ---------------------------------------------------------------------------
# CLI 聊天模式
# ---------------------------------------------------------------------------

def run_cli_chat():
    """交互式 CLI 聊天模式，支持 /skill-name 斜杠命令。"""
    if not (os.getenv("INSTANCE_DIR") or "").strip():
        logger.error("未指定实例目录。请使用 -I <实例名> 启动。\n"
                      "  python run.py -I <实例名>\n"
                      "  nanoghost -I <实例名>")
        return
    if not (os.getenv("LLM_API_KEY") or "").strip():
        if not ensure_llm_configured():
            return
    auto_check_and_notify()

    try:
        import readline  # Unix: 行编辑和 history
    except ImportError:
        try:
            import pyreadline3 as readline  # Windows fallback
        except ImportError:
            pass  # 没有 readline 也不影响基本功能

    db = SqliteDatabase()
    llm = OpenAILLM()
    image_port = SqliteImagePort(db)

    namespace = _clean_env_value(os.getenv("AGENT_NAMESPACE")) or "cli-agent"
    # 技能目录统一由 resolve_instance_skill_dirs 解析（通道那边用的是同一个函数，
    # 免得两边对"实例技能在哪"各有一套说法）
    from agent_core.skill.discovery import resolve_instance_skill_dirs
    _extra_dirs = resolve_instance_skill_dirs()

    agent = Agent(db=db, llm=llm, image_port=image_port, namespace=namespace,
                  skill_extra_dirs=_extra_dirs or None)
    sys_prompt = assemble_sys_prompt()

    session_id = db.create_agent_session("CLI 对话")
    inst_cfg = load_instance_config()
    config = AgentConfig(
        base_url=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        sys_prompt=sys_prompt,
        api_spec={},
        shell_cwd=_clean_env_value(os.getenv("AGENT_WORKDIR")) or None,
        history_max_messages=inst_cfg.history_max_messages,
        history_max_tokens=inst_cfg.history_max_tokens,
    )

    print(f"\n{Style.bold('NanoGhost')} {Style.dim('— AI Agent CLI')}")
    print(f"{Style.dim(f'SKILL.md 技能: {len(agent.list_skill_defs())} 个')}")
    print(f"{Style.dim('/skills 列表  /<name> 加载  /quit 退出')}\n")

    while True:
        try:
            text = input(f"{Style.GREEN}>{Style.RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{Style.dim('再见。')}")
            break

        if not text:
            continue

        if text == "/quit":
            break

        # 斜杠命令：/skills 或 /skill-name
        if text.startswith("/"):
            parts = text[1:].strip().split(maxsplit=1)
            cmd = parts[0]
            rest = parts[1] if len(parts) > 1 else ""

            if cmd == "skills":
                defs = agent.list_skill_defs()
                if not defs:
                    print(f"  {Style.dim('(无可用技能)')}")
                else:
                    print(f"\n{Style.bold(f'可用技能 ({len(defs)})')}:")
                    for s in defs:
                        print(f"  {Style.CYAN}/{s.name}{Style.RESET}  {Style.dim(s.description)}")
                continue

            # /skill-name 直接加载技能
            sd = agent.get_skill_def(cmd)
            if sd:
                print(f"\n{Style.tag('SKILL', sd.name, Style.BLUE)} {Style.dim(sd.description)}")
                print(f"  {Style.dim('─' * 50)}")
                content = agent.skill_registry.load_skill_content(cmd)
                for line in (content or sd.content).split("\n"):
                    print(f"  {line}")
                print(f"  {Style.dim('─' * 50)}")
                if rest:
                    text = rest
                else:
                    continue
            else:
                print(f"  {Style.fail('✗')} 未知技能: {cmd}")
                continue

        print()

        # 流式对话
        for ev_type, ev_data in agent.chat_stream_events(
            user_message=text,
            session_id=session_id,
            config=config,
        ):
            line = _fmt_event(ev_type, ev_data)
            if line:
                print(line, end="" if ev_type in ("text_stream", "subagent_text") else None, flush=True)

        print(f"\n{Style.dim('─' * 40)}\n")


# ---------------------------------------------------------------------------
# System Prompt 组装
# ---------------------------------------------------------------------------

def assemble_sys_prompt() -> str:
    """从 prompts/ 目录加载提示词并组装 system prompt。"""
    inst_prompt_dir = _clean_env_value(os.getenv("AGENT_PROMPTS_DIR"))
    repo_prompt_dir = os.path.join(os.path.dirname(__file__), "prompts")
    prompt_dir = inst_prompt_dir if inst_prompt_dir and os.path.isdir(inst_prompt_dir) else repo_prompt_dir

    parts = []

    # agent_profile
    profile_path = os.path.join(prompt_dir, "agent_profile.md")
    profile = open(profile_path, encoding="utf-8").read().strip() if os.path.exists(profile_path) else ""
    if profile:
        parts.append(profile)

    # agent_rules_conduct
    rules_path = os.path.join(prompt_dir, "agent_rules_conduct.md")
    rules = open(rules_path, encoding="utf-8").read().strip() if os.path.exists(rules_path) else ""
    if rules:
        parts.append(rules)

    # 注入长期记忆；daily memory 改为按轮注入
    inst_dir = _clean_env_value(os.getenv("INSTANCE_DIR"))
    if inst_dir:
        memory_content = read_long_term_memory_block(inst_dir)
        if memory_content:
            parts.append(
                f"## 记住的信息\n\n{memory_content}\n\n"
                f"如需更新，使用 memory_write 工具。"
            )

    sys_prompt = "\n\n".join(parts)
    # 替换占位符（若无 API spec 则会保留原文）
    sys_prompt = sys_prompt.replace("{{agent_api_doc}}", "(无可用 API)")
    sys_prompt = sys_prompt.replace("{{agent_rules_conduct}}", rules or "(无行为规则)")
    return sys_prompt


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_single_turn(message: str, skill_name: Optional[str] = None):
    """单次对话模式。"""
    if not (os.getenv("LLM_API_KEY") or "").strip():
        if not ensure_llm_configured():
            return
    db = SqliteDatabase()
    llm = OpenAILLM()
    image_port = SqliteImagePort(db)

    namespace = _clean_env_value(os.getenv("AGENT_NAMESPACE")) or "cli-agent"
    agent = Agent(db=db, llm=llm, image_port=image_port, namespace=namespace)
    sys_prompt = assemble_sys_prompt()
    session_id = db.create_agent_session("CLI 单次")
    inst_cfg = load_instance_config()
    config = AgentConfig(
        base_url=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        sys_prompt=sys_prompt,
        api_spec={},
        shell_cwd=_clean_env_value(os.getenv("AGENT_WORKDIR")) or None,
        history_max_messages=inst_cfg.history_max_messages,
        history_max_tokens=inst_cfg.history_max_tokens,
    )

    # 预加载技能
    if skill_name:
        sd = agent.get_skill_def(skill_name)
        if sd:
            print(f"  [加载技能: {skill_name}]")
            agent.skill_registry.load_skill_content(skill_name)
        else:
            print(f"  [技能不存在: {skill_name}]")
            return

    for ev_type, ev_data in agent.chat_stream_events(
        user_message=message,
        session_id=session_id,
        config=config,
    ):
        line = _fmt_event(ev_type, ev_data)
        if line:
            print(line, end="" if ev_type in ("text_stream", "subagent_text") else None, flush=True)
    print()


async def run_feishu():
    if not (os.getenv("LLM_API_KEY") or "").strip():
        if not ensure_llm_configured():
            return
    if not os.getenv("FEISHU_APP_ID") or not os.getenv("FEISHU_APP_SECRET"):
        logger.error("飞书模式需要设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        return

    db = SqliteDatabase()
    llm = OpenAILLM()
    image_port = SqliteImagePort(db)

    from agent_core import Agent
    from agent_core.skill.discovery import resolve_instance_skill_dirs
    namespace = _clean_env_value(os.getenv("AGENT_NAMESPACE")) or "feishu-agent"
    # 技能是实例级的，通道也得带上 —— 不带的话实例 <实例>/skills 里的技能对
    # 机器人凭空消失，只剩全局 ~/.agents/skills
    agent = Agent(db=db, llm=llm, image_port=image_port, namespace=namespace,
                  skill_extra_dirs=resolve_instance_skill_dirs() or None)

    sys_prompt = assemble_sys_prompt()
    logger.info("System prompt 长度: %s 字", len(sys_prompt))

    from agent_core.channel.feishu import FeishuWSClient
    ws_client = FeishuWSClient(
        agent=agent,
        sys_prompt=sys_prompt,
        api_spec={},
        base_url=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
    )

    logger.info("Agent 启动完毕, 等待飞书消息...")
    # 定时任务：与飞书渠道并行 —— 到点直接调 run_agent_turn（不经过飞书）。
    # 任务定义见 <实例>/tasks.json；没配置就是纯空转。
    from agent_core.scheduler import scheduler_loop
    await asyncio.gather(
        ws_client.run_forever(),
        scheduler_loop(ws_client),
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NanoGhost Agent CLI")
    parser.add_argument("message", nargs="?", default=None, help="单次对话消息")
    parser.add_argument("--skill", "-s", default=None, help="预加载技能名")
    parser.add_argument("--list-skills", "-l", action="store_true", help="列出所有可用技能")
    parser.add_argument("--instance-dir", "-I", default=None, help="实例目录（多进程多机器人隔离）")
    parser.add_argument("--gateway", action="store_true", help="启动 gateway 常驻服务")
    parser.add_argument("--host", default="127.0.0.1", help="gateway 监听地址")
    parser.add_argument("--port", type=int, default=0, help="gateway 监听端口（必填，>0）")

    args = parser.parse_args()
    mode = os.getenv("AGENT_MODE", "cli").lower()

    if args.gateway:
        if not args.port or int(args.port) <= 0:
            raise SystemExit("--port is required for --gateway")
        from gateway_server import instance_dir_from_env, serve_gateway

        inst = instance_dir_from_env()
        serve_gateway(host=str(args.host), port=int(args.port), instance_dir=inst)
        raise SystemExit(0)

    if mode == "feishu":
        asyncio.run(run_feishu())
    elif args.list_skills:
        # 快速列出技能
        from agent_core.skill.discovery import discover_skills
        skills = discover_skills()
        if not skills:
            print(f"  {Style.dim('(无可用技能)')}")
        else:
            print(f"\n{Style.bold(f'可用技能 ({len(skills)})')}:\n")
            for s in skills:
                print(f"  {Style.CYAN}{s.name}{Style.RESET}")
                print(f"    {Style.dim(s.description)}")
                print(f"    {Style.dim(s.filepath)}\n")
    elif args.skill and not args.message:
        # 查看技能内容
        from agent_core.skill.discovery import discover_skills
        skills = discover_skills()
        found = [s for s in skills if s.name == args.skill]
        if not found:
            print(f"  {Style.fail('✗')} 技能不存在: {args.skill}")
        else:
            sd = found[0]
            print(f"\n{Style.tag('SKILL', sd.name, Style.BLUE)} {Style.dim(sd.description)}")
            print(f"  {Style.dim('─' * 50)}")
            print(sd.content)
    elif args.message:
        run_single_turn(args.message, skill_name=args.skill)
    else:
        run_cli_chat()
