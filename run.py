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
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

# 确保能找到 src/agent_core/
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import requests as http_requests

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("agent_core")

# ---------------------------------------------------------------------------
# 适配器实现
# ---------------------------------------------------------------------------

from agent_core import Agent, AgentConfig
from agent_core.interfaces import DatabasePort, LLMPort, HttpPort, ImagePort, LLMResponse
from agent_core.tool import ToolCall


class SqliteDatabase(DatabasePort):
    """SQLite 实现的 DatabasePort。"""

    def __init__(self, db_path: str = ""):
        self.db_path = db_path or os.path.join(os.path.dirname(__file__), "agent_data.db")
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '新对话',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_messages (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    role TEXT NOT NULL, type TEXT, content TEXT NOT NULL,
                    steps_json TEXT, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_images (
                    id TEXT PRIMARY KEY, base64 TEXT NOT NULL,
                    ref_count INTEGER DEFAULT 1, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_memory_cards (
                    id TEXT PRIMARY KEY, flow_hash TEXT,
                    intent_summary TEXT, intent_examples_json TEXT,
                    intent_vector_json TEXT, flow_signature_json TEXT,
                    steps_json TEXT, success_count INTEGER DEFAULT 0,
                    total_rounds INTEGER DEFAULT 0,
                    approved_count INTEGER DEFAULT 0,
                    rejected_count INTEGER DEFAULT 0,
                    trigger_count INTEGER DEFAULT 0,
                    scene_tag TEXT, namespace TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_memory_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_method TEXT NOT NULL, from_path TEXT NOT NULL,
                    to_method TEXT NOT NULL, to_path TEXT NOT NULL,
                    relation_type TEXT DEFAULT 'FOLLOWS',
                    total_count INTEGER DEFAULT 0,
                    approved_count INTEGER DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(from_method, from_path, to_method, to_path)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_messages_session
                    ON agent_messages(session_id);
            """)
            conn.commit()

    # ---- 会话 ----
    def create_agent_session(self, title="新对话") -> str:
        sid = str(uuid.uuid4())
        now = time.time()
        with self._conn() as conn:
            conn.execute("INSERT INTO agent_sessions (id,title,created_at,updated_at) VALUES (?,?,?,?)",
                         (sid, title[:200], now, now))
            conn.commit()
        return sid

    def get_agent_session(self, session_id):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def update_agent_session_title(self, session_id, title):
        with self._conn() as conn:
            conn.execute("UPDATE agent_sessions SET title=?, updated_at=? WHERE id=?",
                         (title[:200], time.time(), session_id))
            conn.commit()

    def list_agent_sessions(self, limit=50):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM agent_sessions ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_agent_session(self, session_id):
        with self._conn() as conn:
            conn.execute("DELETE FROM agent_messages WHERE session_id=?", (session_id,))
            cur = conn.execute("DELETE FROM agent_sessions WHERE id=?", (session_id,))
            conn.commit()
            return cur.rowcount > 0

    # ---- 消息 ----
    def add_agent_message(self, session_id, role, content, type="text", steps_json=None):
        mid = str(uuid.uuid4())
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO agent_messages (id,session_id,role,type,content,steps_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (mid, session_id, role, type, content, steps_json, now),
            )
            conn.execute("UPDATE agent_sessions SET updated_at=? WHERE id=?", (now, session_id))
            conn.commit()
        return mid

    def get_agent_messages(self, session_id):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, role, type, content, steps_json, created_at FROM agent_messages WHERE session_id=? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_agent_images_batch(self, image_ids):
        if not image_ids:
            return []
        with self._conn() as conn:
            placeholders = ", ".join("?" * len(image_ids))
            rows = conn.execute(
                f"SELECT id, base64, created_at FROM agent_images WHERE id IN ({placeholders})",
                image_ids,
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- 记忆卡片 ----
    def load_all_memory_cards(self, namespace=None):
        with self._conn() as conn:
            if namespace:
                rows = conn.execute(
                    "SELECT * FROM agent_memory_cards WHERE namespace=? ORDER BY updated_at DESC",
                    (namespace,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM agent_memory_cards ORDER BY updated_at DESC").fetchall()
            cards = []
            for row in rows:
                r = dict(row)
                for key, col in [("intent_examples", "intent_examples_json"), ("intent_vector", "intent_vector_json"),
                                  ("flow_signature", "flow_signature_json"), ("steps", "steps_json")]:
                    try:
                        r[key] = json.loads(r.pop(col, "[]"))
                    except Exception:
                        r[key] = []
                cards.append(r)
            return cards

    def save_memory_card(self, card):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO agent_memory_cards (id, flow_hash, intent_summary, intent_examples_json,
                    intent_vector_json, flow_signature_json, steps_json,
                    success_count, total_rounds, approved_count, rejected_count,
                    trigger_count, scene_tag, namespace, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    intent_summary=excluded.intent_summary,
                    intent_examples_json=excluded.intent_examples_json,
                    intent_vector_json=excluded.intent_vector_json,
                    flow_signature_json=excluded.flow_signature_json,
                    steps_json=excluded.steps_json,
                    success_count=excluded.success_count,
                    total_rounds=excluded.total_rounds,
                    approved_count=excluded.approved_count,
                    rejected_count=excluded.rejected_count,
                    trigger_count=excluded.trigger_count,
                    namespace=excluded.namespace,
                    updated_at=excluded.updated_at
            """, (
                card.get("id"), card.get("flow_hash"), card.get("intent_summary"),
                json.dumps(card.get("intent_examples") or [], ensure_ascii=False),
                json.dumps(card.get("intent_vector") or [], ensure_ascii=False),
                json.dumps(card.get("flow_signature") or {}, ensure_ascii=False),
                json.dumps(card.get("steps") or [], ensure_ascii=False),
                int(card.get("success_count") or 0), int(card.get("total_rounds") or 0),
                int(card.get("approved_count") or 0), int(card.get("rejected_count") or 0),
                int(card.get("trigger_count") or 0), card.get("scene_tag"),
                card.get("namespace"), float(card.get("created_at") or 0), float(card.get("updated_at") or 0),
            ))
            conn.commit()

    def delete_memory_card(self, card_id):
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM agent_memory_cards WHERE id=?", (card_id,))
            conn.commit()
            return cur.rowcount > 0

    # ---- 流程图边 ----
    def load_all_memory_edges(self):
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM agent_memory_edges").fetchall()
            return [dict(r) for r in rows]

    def save_memory_edge(self, edge):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO agent_memory_edges (from_method, from_path, to_method, to_path,
                    relation_type, total_count, approved_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(from_method, from_path, to_method, to_path) DO UPDATE SET
                    relation_type=excluded.relation_type, total_count=excluded.total_count,
                    approved_count=excluded.approved_count, updated_at=excluded.updated_at
            """, (
                edge.get("from_method"), edge.get("from_path"),
                edge.get("to_method"), edge.get("to_path"),
                edge.get("relation_type") or "FOLLOWS",
                int(edge.get("total_count") or 0), int(edge.get("approved_count") or 0),
                float(edge.get("created_at") or 0), float(edge.get("updated_at") or 0),
            ))
            conn.commit()


class OpenAILLM(LLMPort):
    """OpenAI 兼容 API 实现的 LLMPort。"""

    def __init__(self):
        self.client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY") or os.getenv("API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL") or os.getenv("BASE_URL"),
        )
        self.model = os.getenv("OPENAI_MODEL") or os.getenv("MODEL_NAME") or "gpt-4o"
        self.embed_model = os.getenv("EMBED_MODEL") or "text-embedding-3-small"

    def stream_chat(self, messages, temperature=0.1) -> Iterator[str]:
        stream = self.client.chat.completions.create(
            model=self.model, messages=messages,
            temperature=temperature, stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    def chat(self, messages, temperature=0.1, tools=None):
        kwargs = dict(model=self.model, messages=messages, temperature=temperature)
        if tools:
            kwargs["tools"] = tools
        response = self.client.chat.completions.create(**kwargs)
        msg = response.choices[0].message

        tool_calls = None
        if msg.tool_calls:
            tool_calls = [ToolCall.from_openai(tc) for tc in msg.tool_calls]

        return LLMResponse(content=msg.content, tool_calls=tool_calls)

    def embed(self, text) -> List[float]:
        resp = self.client.embeddings.create(model=self.embed_model, input=[text])
        return list(resp.data[0].embedding)


class RequestsHttp(HttpPort):
    """requests 实现的 HttpPort。"""

    def request(self, method, url, body=None, timeout=120) -> Tuple[int, Dict]:
        if method == "GET":
            r = http_requests.get(url, timeout=timeout)
        else:
            r = http_requests.request(method, url, json=body or {}, timeout=timeout,
                                       headers={"Content-Type": "application/json"})
        data = r.json() if r.text else {}
        return r.status_code, data


class SqliteImagePort(ImagePort):
    """基于 agent_images 表的 ImagePort。"""

    def __init__(self, db: SqliteDatabase):
        self.db = db

    def add_image(self, base64: str) -> str:
        img_id = f"img-{uuid.uuid4()}"
        now = time.time()
        with self.db._conn() as conn:
            conn.execute("INSERT INTO agent_images (id, base64, ref_count, created_at) VALUES (?, ?, 1, ?)",
                         (img_id, base64, now))
            conn.commit()
        return img_id

    def get_image(self, image_id):
        with self.db._conn() as conn:
            row = conn.execute("SELECT id, base64, created_at FROM agent_images WHERE id=?", (image_id,)).fetchone()
            return dict(row) if row else None

    def get_images_batch(self, ids):
        return self.db.get_agent_images_batch(ids)

    def increment_references(self, ids):
        with self.db._conn() as conn:
            for img_id in ids:
                conn.execute("UPDATE agent_images SET ref_count = ref_count + 1 WHERE id=?", (img_id,))
            conn.commit()

    def decrement_references(self, ids):
        deleted = []
        with self.db._conn() as conn:
            for img_id in ids:
                row = conn.execute("SELECT ref_count FROM agent_images WHERE id=?", (img_id,)).fetchone()
                if not row:
                    continue
                if row["ref_count"] <= 1:
                    conn.execute("DELETE FROM agent_images WHERE id=?", (img_id,))
                    deleted.append(img_id)
                else:
                    conn.execute("UPDATE agent_images SET ref_count = ref_count - 1 WHERE id=?", (img_id,))
            conn.commit()
        return deleted


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

    agent = Agent(db=db, llm=llm, http=http, image_port=image_port, namespace="cli-agent")
    sys_prompt = assemble_sys_prompt()

    session_id = db.create_agent_session("CLI 对话")
    config = AgentConfig(
        base_url=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        sys_prompt=sys_prompt,
        api_spec={},
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
    prompt_dir = os.path.join(os.path.dirname(__file__), "prompts")

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

    agent = Agent(db=db, llm=llm, http=http, image_port=image_port, namespace="cli-agent")
    sys_prompt = assemble_sys_prompt()
    session_id = db.create_agent_session("CLI 单次")
    config = AgentConfig(
        base_url=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        sys_prompt=sys_prompt,
        api_spec={},
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
    agent = Agent(db=db, llm=llm, http=http, image_port=image_port, namespace="feishu-agent")

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

    args = parser.parse_args()
    mode = os.getenv("AGENT_MODE", "cli").lower()

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
