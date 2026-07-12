import asyncio
import sys
from datetime import date as real_date
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for candidate in (str(ROOT), str(SRC)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from agent_core import Agent, AgentConfig
from agent_core.channel.instance import BotInstance
from agent_core.interfaces import DatabasePort, LLMPort, LLMResponse
from agent_core.presenter import run_agent_turn
from run import assemble_sys_prompt
from tests.test_basic import MockDatabase
from tests.test_skill_discovery import ToolCallMockLLM


class StubDatabase(DatabasePort):
    def __init__(self):
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.messages: List[Dict[str, Any]] = []

    def create_agent_session(self, title: str = "新对话") -> str:
        session_id = f"session-{len(self.sessions)}"
        self.sessions[session_id] = {"id": session_id, "title": title}
        return session_id

    def get_agent_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self.sessions.get(session_id)

    def update_agent_session_title(self, session_id: str, title: str) -> None:
        if session_id in self.sessions:
            self.sessions[session_id]["title"] = title

    def list_agent_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        return list(self.sessions.values())[:limit]

    def delete_agent_session(self, session_id: str) -> bool:
        return self.sessions.pop(session_id, None) is not None

    def add_agent_message(
        self,
        session_id: str,
        role: str,
        content: str,
        type: str = "text",
        steps_json: Optional[str] = None,
        reasoning_content: Optional[str] = None,
        root_id: Optional[str] = None,
    ) -> str:
        message_id = f"msg-{len(self.messages)}"
        self.messages.append(
            {
                "id": message_id,
                "session_id": session_id,
                "role": role,
                "content": content,
                "type": type,
                "steps_json": steps_json,
                "reasoning_content": reasoning_content,
                "root_id": root_id,
            }
        )
        return message_id

    def get_agent_messages(self, session_id: str, root_id: Optional[str] = None) -> List[Dict[str, Any]]:
        msgs = [m for m in self.messages if m["session_id"] == session_id]
        if root_id is None:
            return [m for m in msgs if not m.get("root_id")]
        return [m for m in msgs if m.get("root_id") == root_id]

    def get_agent_images_batch(self, image_ids: List[str]) -> List[Dict[str, Any]]:
        return []

    def add_session_image(self, session_id: str, base64: str) -> str:
        return "img-0"

    def get_session_images(self, session_id: str) -> List[Dict[str, Any]]:
        return []

    def load_all_memory_cards(self, namespace: Optional[str] = None) -> List[Dict[str, Any]]:
        return []

    def save_memory_card(self, card: Dict[str, Any]) -> None:
        return None

    def delete_memory_card(self, card_id: str) -> bool:
        return True

    def save_ml_edge(self, edge: Dict[str, Any]) -> None:
        return None

    def load_ml_edges(self, level=None, from_code=None, namespace=None) -> List[Dict[str, Any]]:
        return []

    def load_chat_mentions(self, chat_id: str) -> List[Dict[str, Any]]:
        return []

    def save_chat_mention(self, chat_id: str, name: str, user_id: str) -> None:
        return None

    def delete_chat_mentions(self, chat_id: str) -> None:
        return None


class RecordingLLM(LLMPort):
    def __init__(self):
        self.calls: List[List[Dict[str, Any]]] = []

    def stream_chat(self, messages: List[Dict[str, Any]], temperature: float = 0.1) -> Iterator[str]:
        yield "unused"

    def embed(self, text: str) -> List[float]:
        return [0.0, 0.0, 0.0, 0.0]

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.1,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(content="收到")


class StubSessions:
    def get_or_create(self, chat_id: str):
        return "session-0", True

    def get_context_block(self, source: Any) -> str:
        return "SESSION_CONTEXT"


class StubIO:
    def __init__(self):
        self.replies: List[str] = []
        self.texts: List[str] = []

    def add_reaction(self, message_id: str) -> str:
        return "reaction-1"

    def delete_reaction(self, message_id: str, reaction_id: str) -> None:
        return None

    def reply(self, message_id: str, text: str) -> None:
        self.replies.append(text)

    def send_text(self, chat_id: str, text: str) -> None:
        self.texts.append(text)

    def send_images(self, chat_id: str, images: List[str]) -> None:
        return None


class StubAgent:
    def __init__(self):
        self.seen_config = None

    async def chat_stream_events(self, user_message: str, session_id: str, config: Any, images=None):
        self.seen_config = config
        yield ("done", {"reply": "已处理"})


class FixedDate(real_date):
    @classmethod
    def today(cls):
        return cls(2026, 6, 9)


class Source:
    chat_id = "chat-1"
    thread_id = ""


class Ctx:
    message_id = "message-1"
    root_id = ""


def test_assemble_sys_prompt_reads_only_long_term_memory(tmp_path: Path, monkeypatch):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "agent_profile.md").write_text("PROFILE", encoding="utf-8")
    (prompts / "agent_rules_conduct.md").write_text("RULES", encoding="utf-8")
    (tmp_path / "memory.md").write_text("LONG_TERM", encoding="utf-8")
    daily_dir = tmp_path / "memory.daily"
    daily_dir.mkdir()
    (daily_dir / "2026-06-09.md").write_text("DAILY_ONLY", encoding="utf-8")

    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_PROMPTS_DIR", str(prompts))

    prompt = assemble_sys_prompt()

    assert "LONG_TERM" in prompt
    assert "DAILY_ONLY" not in prompt


def test_bot_instance_refresh_memory_reads_only_long_term(tmp_path: Path):
    (tmp_path / "memory.md").write_text("LONG_TERM", encoding="utf-8")
    daily_dir = tmp_path / "memory.daily"
    daily_dir.mkdir()
    (daily_dir / "2026-06-09.md").write_text("DAILY_ONLY", encoding="utf-8")

    identity = BotInstance("BASE")
    identity.refresh_memory(str(tmp_path))

    prompt = identity.get_base_sys_prompt()
    assert "LONG_TERM" in prompt
    assert "DAILY_ONLY" not in prompt


def test_run_agent_turn_injects_daily_memory_per_turn(tmp_path: Path, monkeypatch):
    daily_dir = tmp_path / "memory.daily"
    daily_dir.mkdir()
    (daily_dir / "2026-06-09.md").write_text("TODAY_MEMORY", encoding="utf-8")

    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    monkeypatch.setattr("agent_core.presenter.date", FixedDate)

    agent = StubAgent()
    identity = BotInstance("BASE_PROMPT")
    io = StubIO()

    reply = asyncio.run(
        run_agent_turn(
            agent=agent,
            identity=identity,
            sessions=StubSessions(),
            io=io,
            context_builder=None,
            source=Source(),
            ctx=Ctx(),
            user_text="你好",
            base_url="http://localhost",
            api_spec={},
        )
    )

    assert reply == ""
    assert io.replies == ["已处理"]
    assert agent.seen_config is not None
    extra_messages = getattr(agent.seen_config, "extra_system_messages", [])
    assert len(extra_messages) == 1
    text = extra_messages[0]["content"][0]["text"]
    assert "TODAY_MEMORY" in text
    assert "SESSION_CONTEXT" not in text


def test_agent_appends_extra_system_messages_before_llm_call():
    db = StubDatabase()
    llm = RecordingLLM()
    agent = Agent(
        db=db,
        llm=llm,
        namespace="test",
        auto_discover_skills=False,
        auto_register_tools=False,
    )
    session_id = db.create_agent_session("test")
    config = AgentConfig(
        base_url="http://localhost",
        sys_prompt="BASE_PROMPT",
        api_spec={},
        extra_system_messages=[
            {
                "role": "system",
                "content": [{"type": "text", "text": "## 今日短期记忆\n\nTODAY_MEMORY"}],
            }
        ],
    )

    async def _collect():
        events = []
        async for event in agent.chat_stream_events(
            user_message="你好",
            session_id=session_id,
            config=config,
        ):
            events.append(event)
        return events

    events = asyncio.run(_collect())

    assert any(event_type == "done" for event_type, _ in events)
    assert llm.calls
    messages = llm.calls[0]
    assert any(
        msg.get("role") == "system"
        and any(part.get("text") == "## 今日短期记忆\n\nTODAY_MEMORY" for part in msg.get("content", []))
        for msg in messages
    )


def test_agent_appends_mcp_awareness_system_message():
    llm = ToolCallMockLLM([
        LLMResponse(content="done"),
    ])
    seen = {}

    def _capture_chat(messages, temperature=0.1, tools=None):
        seen["messages"] = messages
        return LLMResponse(content="done")

    llm.chat = _capture_chat

    agent = Agent(db=MockDatabase(), llm=llm, auto_discover_skills=False)

    class DummyManager:
        def build_awareness_summary(self, instance_dir=None):
            return "## MCP 能力概览\n\n- `capture` (stdio): Capture task system [status=loading]"

    agent._mcp_manager = DummyManager()
    agent.skill_registry.build_skill_context = lambda: None

    config = type(
        "Config",
        (),
        {
            "base_url": "http://localhost",
            "sys_prompt": "You are a helper.",
            "api_spec": {},
            "history_max_messages": 10,
            "history_max_tokens": 1000,
            "root_id": None,
            "extra_system_messages": [],
        },
    )()
    session_id = agent.db.create_agent_session("mcp-awareness")

    async def _run():
        async for _ in agent.chat_stream_events("hello", session_id=session_id, config=config):
            pass

    asyncio.run(_run())
    texts = [
        part.get("text", "")
        for msg in seen["messages"]
        for part in msg.get("content", [])
        if isinstance(part, dict)
    ]
    assert any("MCP 能力概览" in text for text in texts)
    assert any("capture" in text for text in texts)
