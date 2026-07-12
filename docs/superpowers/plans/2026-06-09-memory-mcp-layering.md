# Memory / MCP Layering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate long-term vs daily memory, add MCP awareness/status injection, and keep runtime tool execution gated by actual registered schemas.

**Architecture:** Introduce one small memory helper module and one small MCP awareness path instead of spreading logic across multiple files. Keep stable prompt assembly in `run.py`, per-turn context assembly in `presenter.py`, runtime tool gating in `engine/agent.py`, and MCP status summarization in `mcp/manager.py`.

**Tech Stack:** Python, markdown file storage, existing ToolRegistry/MCPManager infrastructure, pytest

---

## File Structure Map

### New Files

- `src/agent_core/memory/files.py`
  - Centralize long-term vs daily memory path resolution and file reads
- `tests/test_memory_files.py`
  - Unit tests for memory file layout and read helpers
- `tests/test_mcp_awareness.py`
  - Unit tests for MCP capability/status summaries
- `tests/test_prompt_layering.py`
  - Unit tests for stable prompt, daily memory injection, and MCP awareness injection

### Modified Files

- `run.py`
  - Restrict base prompt assembly to stable prompt + long-term memory only
- `src/agent_core/channel/instance.py`
  - Stop mutating `_base_sys_prompt` with daily memory; only refresh long-term memory
- `src/agent_core/presenter.py`
  - Inject today's daily memory block per turn
- `src/agent_core/engine/agent.py`
  - Inject MCP awareness/status block alongside skill awareness before LLM call
- `src/agent_core/mcp/config.py`
  - Parse optional MCP metadata fields for capability descriptions
- `src/agent_core/mcp/manager.py`
  - Build model-facing MCP awareness/status summaries from config + runtime cache
- `src/agent_core/tool/builtins/memory.py`
  - Route writes/reads between `memory.md` and `memory.daily/YYYY-MM-DD.md`

### Files Explicitly Not Restructured

- `src/agent_core/engine/messages.py`
  - Keep history retrieval and image/session augmentation here
- `src/agent_core/tool/registry.py`
  - Keep schema-based callable tool gating unchanged

---

### Task 1: Add File-based Memory Layer Helpers

**Files:**
- Create: `src/agent_core/memory/files.py`
- Test: `tests/test_memory_files.py`

- [ ] **Step 1: Write the failing tests for memory file layout**

```python
from pathlib import Path

from agent_core.memory.files import (
    daily_memory_path,
    ensure_memory_layout,
    long_term_memory_path,
    read_daily_memory_block,
    read_long_term_memory_block,
)


def test_ensure_memory_layout_creates_daily_dir(tmp_path: Path):
    ensure_memory_layout(str(tmp_path))
    assert (tmp_path / "memory.daily").is_dir()


def test_long_term_memory_path_points_to_memory_md(tmp_path: Path):
    assert long_term_memory_path(str(tmp_path)) == tmp_path / "memory.md"


def test_daily_memory_path_uses_iso_date(tmp_path: Path):
    assert daily_memory_path(str(tmp_path), "2026-06-09") == tmp_path / "memory.daily" / "2026-06-09.md"


def test_read_long_term_memory_block_returns_none_for_missing_file(tmp_path: Path):
    assert read_long_term_memory_block(str(tmp_path)) is None


def test_read_daily_memory_block_returns_none_for_missing_day(tmp_path: Path):
    assert read_daily_memory_block(str(tmp_path), "2026-06-09") is None
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run:

```bash
pytest tests/test_memory_files.py -q
```

Expected:

```text
E   ModuleNotFoundError: No module named 'agent_core.memory.files'
```

- [ ] **Step 3: Write the minimal memory file helper module**

```python
from __future__ import annotations

from pathlib import Path
from typing import Optional


def _instance_root(instance_dir: str) -> Path:
    return Path(instance_dir).expanduser().resolve()


def ensure_memory_layout(instance_dir: str) -> None:
    root = _instance_root(instance_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.daily").mkdir(parents=True, exist_ok=True)


def long_term_memory_path(instance_dir: str) -> Path:
    return _instance_root(instance_dir) / "memory.md"


def daily_memory_path(instance_dir: str, day_str: str) -> Path:
    return _instance_root(instance_dir) / "memory.daily" / f"{day_str}.md"


def _read_text(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def read_long_term_memory_block(instance_dir: str) -> Optional[str]:
    return _read_text(long_term_memory_path(instance_dir))


def read_daily_memory_block(instance_dir: str, day_str: str) -> Optional[str]:
    return _read_text(daily_memory_path(instance_dir, day_str))
```

- [ ] **Step 4: Run the tests again**

Run:

```bash
pytest tests/test_memory_files.py -q
```

Expected:

```text
5 passed
```

- [ ] **Step 5: Commit**

```bash
git add src/agent_core/memory/files.py tests/test_memory_files.py
git commit -m "feat: add layered memory file helpers"
```

---

### Task 2: Restrict Base Prompt to Stable Layers and Inject Daily Memory Per Turn

**Files:**
- Modify: `run.py`
- Modify: `src/agent_core/channel/instance.py`
- Modify: `src/agent_core/presenter.py`
- Test: `tests/test_prompt_layering.py`

- [ ] **Step 1: Write the failing tests for stable prompt vs daily prompt**

```python
import os
from pathlib import Path

from run import assemble_sys_prompt


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
```

- [ ] **Step 2: Run the test to verify the current behavior gap**

Run:

```bash
pytest tests/test_prompt_layering.py::test_assemble_sys_prompt_reads_only_long_term_memory -q
```

Expected:

```text
FAIL or ERROR if helper imports / behavior are not yet aligned
```

- [ ] **Step 3: Update `run.py` to use only long-term memory**

```python
from agent_core.memory.files import read_long_term_memory_block


def assemble_sys_prompt() -> str:
    inst_prompt_dir = _clean_env_value(os.getenv("AGENT_PROMPTS_DIR"))
    repo_prompt_dir = os.path.join(os.path.dirname(__file__), "prompts")
    prompt_dir = inst_prompt_dir if inst_prompt_dir and os.path.isdir(inst_prompt_dir) else repo_prompt_dir

    parts = []
    profile_path = os.path.join(prompt_dir, "agent_profile.md")
    profile = open(profile_path, encoding="utf-8").read().strip() if os.path.exists(profile_path) else ""
    if profile:
        parts.append(profile)

    rules_path = os.path.join(prompt_dir, "agent_rules_conduct.md")
    rules = open(rules_path, encoding="utf-8").read().strip() if os.path.exists(rules_path) else ""
    if rules:
        parts.append(rules)

    inst_dir = _clean_env_value(os.getenv("INSTANCE_DIR"))
    if inst_dir:
        long_term = read_long_term_memory_block(inst_dir)
        if long_term:
            parts.append(f"## 记住的信息\n\n{long_term}\n\n如需更新，使用 memory_write 工具。")

    sys_prompt = "\n\n".join(parts)
    sys_prompt = sys_prompt.replace("{{agent_api_doc}}", "(无可用 API)")
    sys_prompt = sys_prompt.replace("{{agent_rules_conduct}}", rules or "(无行为规则)")
    return sys_prompt
```

- [ ] **Step 4: Update `BotInstance` so only long-term memory mutates `_base_sys_prompt`**

```python
from agent_core.memory.files import read_long_term_memory_block


def refresh_memory(self, instance_dir: str = ""):
    if not instance_dir:
        instance_dir = os.environ.get("INSTANCE_DIR", "")
    if not instance_dir:
        return
    memory_content = read_long_term_memory_block(instance_dir)
    if not memory_content:
        return
    marker = "## 记住的信息"
    if marker in self._base_sys_prompt:
        idx = self._base_sys_prompt.find(marker)
        self._base_sys_prompt = self._base_sys_prompt[:idx].rstrip()
    self._base_sys_prompt += "\n\n## 记住的信息\n\n" + memory_content + "\n\n"
```

- [ ] **Step 5: Inject today's daily memory block in `presenter.py`**

```python
from datetime import date

from agent_core.memory.files import read_daily_memory_block


today_str = date.today().isoformat()
daily_memory = read_daily_memory_block(os.environ.get("INSTANCE_DIR", ""), today_str)

config = AgentConfig(
    base_url=base_url,
    sys_prompt=full_sys_prompt,
    api_spec=api_spec or {},
    extra_system_messages=extra_system_blocks,
    history_max_messages=getattr(identity, "history_max_messages", 120),
    history_max_tokens=getattr(identity, "history_max_tokens", 200_000),
    root_id=root_key or None,
)

extra_system_blocks = []
if daily_memory:
    extra_system_blocks.append({
        "role": "system",
        "content": [{"type": "text", "text": f"## 今日短期记忆\n\n{daily_memory}"}],
    })
```

- [ ] **Step 6: Thread the extra system blocks into the Agent call path**

```python
@dataclass
class AgentConfig:
    base_url: str
    sys_prompt: str
    api_spec: Dict[str, Any] = field(default_factory=dict)
    extra_system_messages: List[Dict[str, Any]] = field(default_factory=list)
```

```python
messages = await asyncio.to_thread(
    build_agent_messages_with_history,
    session_id=session_id,
    sys_prompt=config.sys_prompt,
    user_message=user_message,
    db=self.agent.db,
    user_images=images,
    stored_image_ids=stored_image_ids or None,
    llm=self.agent.llm,
    namespace=self.agent.namespace,
    history_max_messages=getattr(config, "history_max_messages", 120),
    history_max_tokens=getattr(config, "history_max_tokens", 200_000),
    root_id=getattr(config, "root_id", None),
    supports_vision=self.agent.llm.supports_vision,
)
if getattr(config, "extra_system_messages", None):
    messages.extend(config.extra_system_messages)
```

- [ ] **Step 7: Run prompt-layering tests**

Run:

```bash
pytest tests/test_prompt_layering.py -q
```

Expected:

```text
all prompt layering tests pass
```

- [ ] **Step 8: Commit**

```bash
git add run.py src/agent_core/channel/instance.py src/agent_core/presenter.py src/agent_core/config.py src/agent_core/engine/agent.py tests/test_prompt_layering.py
git commit -m "feat: split stable prompt and daily memory injection"
```

---

### Task 3: Add MCP Awareness Metadata and Runtime Status Summary

**Files:**
- Modify: `src/agent_core/mcp/config.py`
- Modify: `src/agent_core/mcp/manager.py`
- Test: `tests/test_mcp_awareness.py`

- [ ] **Step 1: Write failing tests for MCP awareness summaries**

```python
from pathlib import Path

from agent_core.mcp.config import resolve_servers
from agent_core.mcp.manager import MCPManager


def test_build_awareness_summary_includes_enabled_server_description(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NANOGHOST_GLOBAL_CONFIG", str(tmp_path / "global.yaml"))
    (tmp_path / "global.yaml").write_text(
        "mcp_servers:\n"
        "  capture:\n"
        "    transport: stdio\n"
        "    command: python\n"
        "    args: ['capture.py']\n"
        "    description: Capture task system\n",
        encoding="utf-8",
    )
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / "config.yaml").write_text("mcp:\n  enabled_only:\n    - capture\n", encoding="utf-8")

    mgr = MCPManager()
    summary = mgr.build_awareness_summary(inst)

    assert "capture" in summary
    assert "Capture task system" in summary
```

- [ ] **Step 2: Run the awareness test to verify it fails**

Run:

```bash
pytest tests/test_mcp_awareness.py::test_build_awareness_summary_includes_enabled_server_description -q
```

Expected:

```text
E   AttributeError: 'MCPManager' object has no attribute 'build_awareness_summary'
```

- [ ] **Step 3: Extend MCP config parsing with optional metadata**

```python
@dataclass(frozen=True)
class MCPServerConfig:
    server_id: str
    transport: str
    url: str
    headers: Dict[str, str]
    timeout_seconds: int
    extra_args: List[str] = None
    title: str = ""
    description: str = ""
    use_cases: List[str] = None
    notes: str = ""
```

```python
title = str(s.get("title") or sid).strip()
description = str(s.get("description") or "").strip()
use_cases_raw = _as_list(s.get("use_cases"))
use_cases = [str(x).strip() for x in use_cases_raw if str(x).strip()]
notes = str(s.get("notes") or "").strip()
```

- [ ] **Step 4: Add a concise MCP awareness/status summary builder in `manager.py`**

```python
def build_awareness_summary(self, instance_dir: Optional[Path] = None) -> Optional[str]:
    inst = instance_dir or _instance_dir_from_env()
    if inst is None:
        return None
    with self._lock:
        self._ensure_loaded(inst)
        servers = list(self._servers.values())
        cache_map = dict(self._cache)
    if not servers:
        return None
    lines = ["## MCP 能力概览", ""]
    for cfg in servers:
        cache = cache_map.get(cfg.server_id) or ServerCache(tools={})
        status = "ready" if cache.status == "connected" and cache.tools else (
            "error" if cache.status == "error" else "loading"
        )
        desc = cfg.description or "已启用 MCP 服务"
        lines.append(f"- `{cfg.server_id}` ({cfg.transport}): {desc} [status={status}]")
        if cfg.use_cases:
            lines.append(f"  适用: {', '.join(cfg.use_cases[:3])}")
    return "\n".join(lines)
```

- [ ] **Step 5: Run MCP awareness tests**

Run:

```bash
pytest tests/test_mcp_awareness.py -q
```

Expected:

```text
all MCP awareness tests pass
```

- [ ] **Step 6: Commit**

```bash
git add src/agent_core/mcp/config.py src/agent_core/mcp/manager.py tests/test_mcp_awareness.py
git commit -m "feat: add MCP awareness and status summaries"
```

---

### Task 4: Inject MCP Awareness Next to Skill Awareness Without Changing Tool Gating

**Files:**
- Modify: `src/agent_core/engine/agent.py`
- Test: `tests/test_prompt_layering.py`

- [ ] **Step 1: Write the failing test for MCP awareness injection**

```python
import asyncio

from agent_core import ToolCall
from agent_core.interfaces import LLMResponse
from tests.test_basic import MockDatabase
from tests.test_skill_discovery import ToolCallMockLLM


def test_agent_appends_mcp_awareness_system_message():
    llm = ToolCallMockLLM([
        LLMResponse(content="done"),
    ])
    seen = {}

    def _capture_chat(messages, temperature=0.1, tools=None):
        seen["messages"] = messages
        return LLMResponse(content="done")

    llm.chat = _capture_chat

    from agent_core import Agent
    agent = Agent(db=MockDatabase(), llm=llm, auto_discover_skills=False)

    class DummyManager:
        def build_awareness_summary(self, instance_dir=None):
            return "## MCP 能力概览\n\n- `capture` (stdio): Capture task system [status=loading]"

    agent._mcp_manager = DummyManager()
    agent.skill_registry.build_skill_context = lambda: None

    config = type("Config", (), {
        "base_url": "http://localhost",
        "sys_prompt": "You are a helper.",
        "api_spec": {},
        "history_max_messages": 10,
        "history_max_tokens": 1000,
        "root_id": None,
        "extra_system_messages": [],
    })()
    session_id = agent.db.create_agent_session("mcp-awareness")

    async def _run():
        async for _ in agent.chat_stream_events("hello", session_id=session_id, config=config):
            pass

    asyncio.run(_run())
    texts = [part.get("text", "") for msg in seen["messages"] for part in msg.get("content", []) if isinstance(part, dict)]
    assert any("MCP 能力概览" in text for text in texts)
    assert any("capture" in text for text in texts)
```

- [ ] **Step 2: Run the test to verify the injection path is not yet asserted**

Run:

```bash
pytest tests/test_prompt_layering.py::test_agent_appends_mcp_awareness_system_message -q
```

Expected:

```text
FAIL because no MCP awareness block is appended into LLM messages yet
```

- [ ] **Step 3: Inject MCP awareness block before the LLM call**

```python
skill_block = self.agent.skill_registry.build_skill_context()
if skill_block:
    messages.append({
        "role": "system",
        "content": [{"type": "text", "text": skill_block}],
    })

mcp_block = None
if getattr(self.agent, "_mcp_manager", None) is not None:
    try:
        mcp_block = self.agent._mcp_manager.build_awareness_summary()
    except Exception:
        mcp_block = None
if mcp_block:
    messages.append({
        "role": "system",
        "content": [{"type": "text", "text": mcp_block}],
    })
```

- [ ] **Step 4: Verify runtime tool gating is unchanged**

Run:

```bash
pytest tests/test_skill_discovery.py::test_tool_auto_register_default -q
```

Expected:

```text
PASS
```

- [ ] **Step 5: Run all prompt-layering tests**

Run:

```bash
pytest tests/test_prompt_layering.py -q
```

Expected:

```text
all prompt-layering tests pass
```

- [ ] **Step 6: Commit**

```bash
git add src/agent_core/engine/agent.py tests/test_prompt_layering.py
git commit -m "feat: inject MCP awareness into LLM context"
```

---

### Task 5: Route Memory Read/Write Between Long-term and Daily Stores

**Files:**
- Modify: `src/agent_core/tool/builtins/memory.py`
- Test: `tests/test_memory_files.py`

- [ ] **Step 1: Write failing tests for target-based memory writes**

```python
import os
from pathlib import Path

from agent_core.tool.builtins.memory import memory_write


def test_memory_write_append_daily_target(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    result = memory_write(
        {"action": "append", "section": "daily_log", "content": "跟进 capture 状态", "target": "daily"},
        {},
    )
    assert result.ok
    daily_dir = tmp_path / "memory.daily"
    assert daily_dir.is_dir()
    assert list(daily_dir.glob("*.md"))
```

- [ ] **Step 2: Run the new test to verify it fails**

Run:

```bash
pytest tests/test_memory_files.py::test_memory_write_append_daily_target -q
```

Expected:

```text
FAIL because `target` routing is not implemented yet
```

- [ ] **Step 3: Extend `MEMORY_WRITE_DEF` with explicit target routing**

```python
MEMORY_WRITE_DEF = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["append", "update", "delete"]},
        "section": {"type": "string"},
        "content": {"type": "string"},
        "key": {"type": "string"},
        "target": {
            "type": "string",
            "enum": ["long_term", "daily"],
            "description": "Write to memory.md or today's daily memory file",
            "default": "long_term",
        },
    },
    "required": ["action", "section"],
}
```

- [ ] **Step 4: Route the write path using the new helper module**

```python
from datetime import date

from agent_core.memory.files import daily_memory_path, ensure_memory_layout, long_term_memory_path


target = args.get("target", "long_term")
ensure_memory_layout(inst_dir)
if target == "daily":
    path = daily_memory_path(inst_dir, date.today().isoformat())
else:
    path = long_term_memory_path(inst_dir)
```

- [ ] **Step 5: Run the focused tests**

Run:

```bash
pytest tests/test_memory_files.py -q
```

Expected:

```text
all memory file tests pass
```

- [ ] **Step 6: Commit**

```bash
git add src/agent_core/tool/builtins/memory.py tests/test_memory_files.py
git commit -m "feat: route memory writes to long-term or daily stores"
```

---

### Task 6: Final Verification and Progress-oriented Handoff

**Files:**
- Modify: `docs/usage.md`
- Test: existing focused test set

- [ ] **Step 1: Add a short usage note for the new memory and MCP behavior**

```md
## Memory and MCP Context

- `memory.md` stores long-term descriptive memory
- `memory.daily/YYYY-MM-DD.md` stores same-day working memory
- Enabled MCP servers may appear in context before they are callable
- Only tools present in runtime tool schemas are executable
```

- [ ] **Step 2: Run the focused regression suite**

Run:

```bash
pytest tests/test_memory_files.py tests/test_mcp_awareness.py tests/test_prompt_layering.py tests/test_skill_discovery.py -q
```

Expected:

```text
all targeted tests pass
```

- [ ] **Step 3: Run one end-to-end smoke check for MCP awareness**

Run:

```bash
python -c "import os; os.environ['INSTANCE_DIR']=r'C:\Users\Administrator\.nanoghost\instances\cc'; from dotenv import load_dotenv; load_dotenv(r'C:\Users\Administrator\.nanoghost\instances\cc\.env', override=True); from agent_core import Agent; from agent_core.adapters import SqliteDatabase, OpenAILLM, SqliteImagePort; a=Agent(db=SqliteDatabase(), llm=OpenAILLM(), image_port=SqliteImagePort(SqliteDatabase()), namespace='cc-smoke'); import time; time.sleep(3); print([x['function']['name'] for x in a.tool_registry.get_available_schemas() if 'mcp' in x['function']['name']])"
```

Expected:

```text
['mcp_capture']
```

- [ ] **Step 4: Prepare the final progress/status handoff message**

```text
已完成：
- 长期记忆与当日记忆分层
- MCP 能力认知与运行状态注入
- schema 级可调用约束保持不变

当前现状：
- 首轮可认知 enabled MCP
- ready 前不会误判为可调用
- memory.md 不再承载当天工作记忆
```

- [ ] **Step 5: Commit**

```bash
git add docs/usage.md
git commit -m "docs: explain layered memory and MCP awareness"
```

---

## Self-Review

### Spec Coverage

- Layered memory model: covered by Tasks 1, 2, 5
- MCP awareness vs readiness split: covered by Tasks 3, 4
- Prompt assembly order: covered by Tasks 2, 4
- File-level boundary clarity: reflected in File Structure Map and task ownership
- Progress/current-state feedback after implementation: covered by Task 6

### Placeholder Scan

- No `TODO`, `TBD`, or "implement later" placeholders remain
- Each code-changing step includes concrete code
- Each verification step includes exact commands and expected outcomes

### Type Consistency

- `AgentConfig.extra_system_messages` is introduced once and referenced consistently
- `memory.daily/YYYY-MM-DD.md` path format is consistent across helper, presenter, and tool routing tasks
- MCP awareness builder is consistently named `build_awareness_summary()`

---

**Plan complete and saved to `docs/superpowers/plans/2026-06-09-memory-mcp-layering.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
