import asyncio
"""Basic smoke tests for agent-core."""

from typing import Any, Dict, List, Iterator, Optional

from agent_core import Agent, AgentConfig
from agent_core.interfaces import DatabasePort, LLMPort


# ---- Mock Ports ----


class MockDatabase(DatabasePort):
    def __init__(self):
        self.sessions = {}
        self.messages = {}
        self.cards = []
        self.edges = []

    def create_agent_session(self, title="新对话") -> str:
        sid = f"test-session-{len(self.sessions)}"
        self.sessions[sid] = {"id": sid, "title": title}
        return sid

    def get_agent_session(self, session_id):
        return self.sessions.get(session_id)

    def update_agent_session_title(self, session_id, title):
        if session_id in self.sessions:
            self.sessions[session_id]["title"] = title

    def list_agent_sessions(self, limit=50):
        return list(self.sessions.values())[:limit]

    def delete_agent_session(self, session_id):
        return self.sessions.pop(session_id, None) is not None

    def add_agent_message(self, session_id, role, content, type="text", steps_json=None, reasoning_content=None, root_id=None):
        mid = f"msg-{len(self.messages)}"
        self.messages[mid] = {"session_id": session_id, "role": role, "content": content, "type": type, "steps_json": steps_json, "root_id": root_id}
        if session_id in self.sessions:
            self.sessions[session_id]["messages"] = self.sessions[session_id].get("messages", []) + [mid]
        return mid

    def get_agent_messages(self, session_id, root_id=None):
        all_msgs = [v for k, v in self.messages.items() if v["session_id"] == session_id]
        if root_id:
            return [m for m in all_msgs if m.get("root_id") == root_id]
        return [m for m in all_msgs if not m.get("root_id")]

    def get_agent_images_batch(self, image_ids):
        return []

    def load_all_memory_cards(self, namespace=None):
        if namespace:
            return [c for c in self.cards if c.get("namespace") == namespace]
        return self.cards

    def save_memory_card(self, card):
        for i, c in enumerate(self.cards):
            if c.get("id") == card.get("id"):
                self.cards[i] = card
                return
        self.cards.append(card)

    def delete_memory_card(self, card_id):
        self.cards = [c for c in self.cards if c.get("id") != card_id]
        return True

    def save_ml_edge(self, edge):
        pass

    def load_ml_edges(self, level=None, from_code=None, namespace=None):
        return []

    def load_chat_mentions(self, chat_id):
        return []

    def save_chat_mention(self, chat_id, name, user_id):
        pass

    def delete_chat_mentions(self, chat_id):
        pass

class MockLLM(LLMPort):
    def __init__(self, responses=None):
        self.responses = responses or []

    def stream_chat(self, messages, temperature=0.1) -> Iterator[str]:
        if self.responses:
            yield self.responses.pop(0)
        else:
            yield '{"thought": "test", "done": true, "reply": "Hello from mock LLM"}'

    def embed(self, text) -> List[float]:
        return [0.1] * 4  # minimal vector


# ---- Tests ----


def test_agent_instantiation():
    db = MockDatabase()
    llm = MockLLM()
    agent = Agent(db=db, llm=llm, namespace="test")
    assert agent.namespace == "test"
    assert agent.db is db
    assert agent.llm is llm
    print("  [OK] Agent instantiation")


def test_agent_chat_stream_events():
    db = MockDatabase()
    llm = MockLLM(responses=['{"thought": "ok", "done": true, "reply": "任务已创建。"}'])
    agent = Agent(db=db, llm=llm, namespace="test")

    config = AgentConfig(
        base_url="http://localhost:8000",
        sys_prompt="你是助手",
        api_spec={"schemas": [], "details": {}},
    )
    session_id = db.create_agent_session("test")

    async def _collect():
        _evs = []
        async for _ev in agent.chat_stream_events(
            user_message="帮我创建一个任务",
            session_id=session_id,
            config=config,
        ):
            _evs.append(_ev)
        return _evs

    events = asyncio.run(_collect())

    event_types = [e[0] for e in events]
    assert "session" in event_types
    assert "text_stream" in event_types
    assert "done" in event_types
    print(f"  [OK] chat_stream_events yields: {event_types}")


def test_namespace_isolation():
    db = MockDatabase()
    llm = MockLLM()

    agent_a = Agent(db=db, llm=llm, namespace="app_a")
    agent_b = Agent(db=db, llm=llm, namespace="app_b")

    # Record a flow under agent_a's namespace
    from agent_core.memory.cards import record_successful_flow
    record_successful_flow(
        user_intent="task for A",
        steps=[{"step": 1, "method": "GET", "path": "/api/tasks", "ok": True}],
        rounds_used=1,
        db=db, llm=llm, namespace="app_a",
    )

    cards_a = db.load_all_memory_cards(namespace="app_a")
    cards_b = db.load_all_memory_cards(namespace="app_b")
    cards_all = db.load_all_memory_cards(namespace=None)

    assert len(cards_a) == 1
    assert len(cards_b) == 0
    assert len(cards_all) == 1
    print(f"  [OK] Namespace isolation: A={len(cards_a)} B={len(cards_b)}")


def test_sub_agent():
    db = MockDatabase()
    llm = MockLLM()

    parent = Agent(db=db, llm=llm, namespace="parent")
    child = parent.create_sub_agent("child-1")

    assert child.namespace is not None
    assert "child" in child.namespace
    assert child.db is parent.db
    assert child.llm is parent.llm
    assert parent.get_sub_agent("child-1") is child
    print(f"  [OK] SubAgent created with namespace: {child.namespace}")


if __name__ == "__main__":
    test_agent_instantiation()
    test_agent_chat_stream_events()
    test_namespace_isolation()
    test_sub_agent()
    print("\n[OK] All tests passed!")
