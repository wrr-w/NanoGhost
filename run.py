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

    # 飞书模式
    set AGENT_MODE=feishu
    python run.py
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
    if not instance_dir:
        load_dotenv()
        return

    instance_dir = os.path.abspath(os.path.expanduser(instance_dir))
    os.environ["INSTANCE_DIR"] = instance_dir

    os.makedirs(os.path.join(instance_dir, "data"), exist_ok=True)
    os.makedirs(os.path.join(instance_dir, "work"), exist_ok=True)
    os.makedirs(os.path.join(instance_dir, "skills"), exist_ok=True)

    os.environ.setdefault("AGENT_DB_PATH", os.path.join(instance_dir, "data", "agent_data.db"))
    os.environ.setdefault("AGENT_PROMPTS_DIR", os.path.join(instance_dir, "prompts"))
    os.environ.setdefault("AGENT_WORKDIR", os.path.join(instance_dir, "work"))
    os.environ.setdefault("AGENTS_SKILLS_DIR", os.path.join(instance_dir, "skills"))
    os.environ.setdefault("AGENT_NAMESPACE", os.path.basename(instance_dir.rstrip("\\/")) or "agent")

    dotenv_path = os.path.join(instance_dir, ".env")
    if os.path.isfile(dotenv_path):
        load_dotenv(dotenv_path=dotenv_path)
    else:
        load_dotenv()


_bootstrap_instance(sys.argv)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("agent_core")

# 适配器已提取至 agent_core.adapters
from agent_core import Agent, AgentConfig
from agent_core.adapters import SqliteDatabase, OpenAILLM, RequestsHttp, SqliteImagePort
from agent_core.tool import ToolCall

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
    try:
        import readline  # Unix: 行编辑和 history
    except ImportError:
        try:
            import pyreadline3 as readline  # Windows fallback
        except ImportError:
            pass  # 没有 readline 也不影响基本功能

    db = SqliteDatabase()
    llm = OpenAILLM()
    http = RequestsHttp()
    image_port = SqliteImagePort(db)

    namespace = _clean_env_value(os.getenv("AGENT_NAMESPACE")) or "cli-agent"
    # 从 config.yaml 读取技能搜索目录
    import os as _sk_os
    from agent_core.utils.yaml_subset import load_yaml_subset as _sk_load
    _cfg = _sk_load(_sk_os.path.join(_sk_os.environ.get("INSTANCE_DIR", ""), "config.yaml"))
    _sk_dirs = _cfg.get("skills", {}).get("extra_dirs", []) if isinstance(_cfg, dict) else []
    _extra_dirs = []
    for _d in (_sk_dirs if isinstance(_sk_dirs, list) else []):
        _p = _sk_os.path.expanduser(str(_d).strip())
        if _p and _sk_os.path.isdir(_p):
            _extra_dirs.append(_p)
    agent = Agent(db=db, llm=llm, http=http, image_port=image_port, namespace=namespace,
                  skill_extra_dirs=_extra_dirs or None)
    sys_prompt = assemble_sys_prompt()

    session_id = db.create_agent_session("CLI 对话")
    config = AgentConfig(
        base_url=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        sys_prompt=sys_prompt,
        api_spec={},
        shell_cwd=_clean_env_value(os.getenv("AGENT_WORKDIR")) or None,
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

    # 注入 memory.md
    inst_dir = _clean_env_value(os.getenv("INSTANCE_DIR"))
    if inst_dir:
        memory_path = os.path.join(inst_dir, "memory.md")
        if os.path.isfile(memory_path):
            try:
                with open(memory_path, encoding="utf-8") as f:
                    memory_content = f.read().strip()
                if memory_content:
                    parts.append(
                        f"## 记住的信息\n\n{memory_content}\n\n"
                        f"如需更新，使用 memory_write 工具。"
                    )
            except Exception:
                pass

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
    db = SqliteDatabase()
    llm = OpenAILLM()
    http = RequestsHttp()
    image_port = SqliteImagePort(db)

    namespace = _clean_env_value(os.getenv("AGENT_NAMESPACE")) or "cli-agent"
    agent = Agent(db=db, llm=llm, http=http, image_port=image_port, namespace=namespace)
    sys_prompt = assemble_sys_prompt()
    session_id = db.create_agent_session("CLI 单次")
    config = AgentConfig(
        base_url=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        sys_prompt=sys_prompt,
        api_spec={},
        shell_cwd=_clean_env_value(os.getenv("AGENT_WORKDIR")) or None,
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
    if not os.getenv("FEISHU_APP_ID") or not os.getenv("FEISHU_APP_SECRET"):
        logger.error("飞书模式需要设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        return

    db = SqliteDatabase()
    llm = OpenAILLM()
    http = RequestsHttp()
    image_port = SqliteImagePort(db)

    from agent_core import Agent
    namespace = _clean_env_value(os.getenv("AGENT_NAMESPACE")) or "feishu-agent"
    agent = Agent(db=db, llm=llm, http=http, image_port=image_port, namespace=namespace)

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
    await ws_client.run_forever()


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
        from agent_core.skill.discovery import SEARCH_DIRS
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
