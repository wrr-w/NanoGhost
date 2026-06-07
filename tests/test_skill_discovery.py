import asyncio
"""Tests for SKILL.md discovery, ecosystem compatibility, and tool calling."""

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from agent_core.interfaces import LLMPort, LLMResponse

# Must import Mock classes at module level for ToolCallMockLLM subclass
from tests.test_basic import MockLLM, MockDatabase  # noqa: E402

from agent_core.skill import (
    SkillDefinition,
    SkillRegistry,
    discover_skills,
    load_skill_from_dir,
)
from agent_core import Agent, ToolCall, ToolResult, ToolRegistry, register_builtins


def _create_skill_md(tmpdir: str, name: str, description: str,
                     extra_frontmatter: str = "",
                     body: str = "## What I do\n- Task A\n- Task B") -> str:
    """Create a temporary SKILL.md file and return its directory path."""
    skill_dir = os.path.join(tmpdir, name)
    os.makedirs(skill_dir, exist_ok=True)
    content = (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{extra_frontmatter}"
        f"---\n"
        f"{body}\n"
    )
    Path(os.path.join(skill_dir, "SKILL.md")).write_text(content, encoding="utf-8")
    return skill_dir


def test_parse_basic_frontmatter():
    """Parse standard SKILL.md with basic frontmatter."""
    raw = """---
name: git-release
description: Create consistent releases and changelogs
license: MIT
compatibility: opencode
---
## What I do
Draft release notes from merged PRs.
"""
    from agent_core.skill.discovery import _parse_frontmatter
    fm, content = _parse_frontmatter(raw)

    assert fm["name"] == "git-release"
    assert fm["description"] == "Create consistent releases and changelogs"
    assert fm["license"] == "MIT"
    assert fm["compatibility"] == "opencode"
    assert "## What I do" in content
    assert "Draft release notes" in content


def test_parse_nested_metadata():
    """Parse frontmatter with nested metadata section."""
    raw = """---
name: code-review
description: Review pull requests
metadata:
  audience: developers
  workflow: github
---
Review checklist here.
"""
    from agent_core.skill.discovery import _parse_frontmatter
    fm, content = _parse_frontmatter(raw)

    assert fm["name"] == "code-review"
    assert fm["description"] == "Review pull requests"
    assert isinstance(fm.get("metadata"), dict)
    assert fm["metadata"].get("audience") == "developers"
    assert fm["metadata"].get("workflow") == "github"


def test_parse_hermes_frontmatter():
    """Parse frontmatter with Hermes-style metadata.hermes block."""
    raw = """---
name: spike
description: Throwaway experiments
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [spike, prototype]
    related_skills: [writing-plans]
---
# Spike
"""
    from agent_core.skill.discovery import _parse_frontmatter, load_skill_from_dir
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = os.path.join(tmpdir, "test-skill")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(raw)

        sd = load_skill_from_dir(skill_dir)
        assert sd is not None
        assert sd.name == "spike"
        assert sd.version == "1.0.0"
        assert sd.platforms == ["linux", "macos", "windows"]
        assert "spike" in sd.tags
        assert "prototype" in sd.tags
        assert "writing-plans" in sd.related_skills
        # Nested hermes fields should be flattened in metadata
        assert sd.metadata.get("hermes.tags") is not None


def test_parse_no_frontmatter():
    """File without frontmatter returns empty dict and full text as content."""
    raw = "Just a plain markdown file."
    from agent_core.skill.discovery import _parse_frontmatter
    fm, content = _parse_frontmatter(raw)
    assert fm == {}
    assert content == "Just a plain markdown file."


def test_parse_partial_frontmatter():
    """Only opening --- without closing returns full text as content."""
    raw = "---\nname: test\nSome content"
    from agent_core.skill.discovery import _parse_frontmatter
    fm, content = _parse_frontmatter(raw)
    assert fm == {}
    assert content == raw.strip()


def test_parse_missing_required_fields():
    """Missing name or description returns None from load_skill_from_dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = os.path.join(tmpdir, "no-name")
        os.makedirs(skill_dir)
        Path(os.path.join(skill_dir, "SKILL.md")).write_text(
            "---\ndescription: desc\n---\nContent", encoding="utf-8"
        )
        assert load_skill_from_dir(skill_dir) is None

        skill_dir2 = os.path.join(tmpdir, "no-fm")
        os.makedirs(skill_dir2)
        Path(os.path.join(skill_dir2, "SKILL.md")).write_text(
            "Just content", encoding="utf-8"
        )
        assert load_skill_from_dir(skill_dir2) is None


def test_discover_skills_from_extra_dir():
    """Discover skills from an extra directory includes our test skills."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_skill_md(tmpdir, "git-release", "Create releases")
        _create_skill_md(tmpdir, "code-review", "Review PRs",
                         extra_frontmatter="license: MIT\n")

        skills = discover_skills(extra_dirs=[tmpdir])
        names_found = {s.name for s in skills}
        assert "git-release" in names_found
        assert "code-review" in names_found


def test_discover_dedup_by_name():
    """Same skill name from later directories is ignored."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dir1 = os.path.join(tmpdir, "dir1")
        dir2 = os.path.join(tmpdir, "dir2")
        os.makedirs(dir1)
        os.makedirs(dir2)

        _create_skill_md(dir1, "doc-gen", "Generate docs v1",
                         extra_frontmatter="compatibility: opencode\n")
        _create_skill_md(dir2, "doc-gen", "Generate docs v2")

        skills = discover_skills(extra_dirs=[dir1, dir2])
        doc_skills = [s for s in skills if s.name == "doc-gen"]
        assert len(doc_skills) == 1
        assert doc_skills[0].description == "Generate docs v1"
        assert dir1 in doc_skills[0].filepath


def test_load_skill_from_dir():
    """Load a specific skill from directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = _create_skill_md(
            tmpdir, "test-skill", "A test skill",
            extra_frontmatter="license: MIT\ncompatibility: opencode\n",
            body="## Instructions\nDo something.\n",
        )
        sd = load_skill_from_dir(skill_dir)
        assert sd is not None
        assert sd.name == "test-skill"
        assert sd.description == "A test skill"
        assert sd.license == "MIT"
        assert sd.compatibility == "opencode"
        assert "## Instructions" in sd.content
        assert "Do something." in sd.content


def test_registry_discover():
    """Registry.discover() loads skills into the registry."""
    registry = SkillRegistry()
    with tempfile.TemporaryDirectory() as tmpdir:
        _create_skill_md(tmpdir, "skill-a", "Skill A")
        _create_skill_md(tmpdir, "skill-b", "Skill B",
                         extra_frontmatter="license: Apache-2.0\n")

        count = registry.discover(extra_dirs=[tmpdir])
        names = {s.name for s in registry.list_skill_defs()}
        assert "skill-a" in names
        assert "skill-b" in names
        assert count == len(registry.list_skill_defs())


def test_registry_empty():
    """Empty registry returns empty lists."""
    registry = SkillRegistry()
    assert registry.list_skill_defs() == []
    assert registry.match_skills("anything") == []
    assert registry.build_skill_context() is None
    assert registry.get_skill_def("nonexistent") is None
    assert registry.load_skill_content("nonexistent") is None


def test_registry_match_skills():
    """match_skills returns relevant skills by keyword."""
    registry = SkillRegistry()
    registry.add_skill_def(SkillDefinition(
        name="git-release", description="Create releases and changelogs",
        content="Release instructions", filepath="/fake/git-release/SKILL.md",
    ))
    registry.add_skill_def(SkillDefinition(
        name="code-review", description="Review pull requests for quality",
        content="Review instructions", filepath="/fake/code-review/SKILL.md",
    ))
    registry.add_skill_def(SkillDefinition(
        name="deploy", description="Deploy to production",
        content="Deploy instructions", filepath="/fake/deploy/SKILL.md",
    ))

    matched = registry.match_skills("need to review a PR", top_k=2)
    assert len(matched) == 2
    assert matched[0].name == "code-review"

    matched2 = registry.match_skills("create a new release")
    assert matched2[0].name == "git-release"


def test_registry_build_skill_context():
    """build_skill_context returns lightweight index (name+description only)."""
    registry = SkillRegistry()
    registry.add_skill_def(SkillDefinition(
        name="git-release", description="Create releases",
        content="## Steps\n1. Tag\n2. Release",
        filepath="/fake/git-release/SKILL.md",
    ))

    ctx = registry.build_skill_context()
    assert ctx is not None
    assert "git-release" in ctx
    assert "Create releases" in ctx
    assert "<available_skills>" in ctx
    assert "use_skill" in ctx
    # Should NOT contain full skill content
    assert "## Steps" not in ctx
    assert "1. Tag" not in ctx


def test_registry_load_skill_content():
    """load_skill_content returns full content for existing skill, None for missing."""
    registry = SkillRegistry()
    registry.add_skill_def(SkillDefinition(
        name="git-release", description="Create releases",
        content="## Steps\n1. Tag\n2. Release",
        filepath="/fake/git-release/SKILL.md",
    ))
    content = registry.load_skill_content("git-release")
    assert content is not None
    assert "git-release" in content
    assert "Create releases" in content
    assert "## Steps" in content
    assert "1. Tag" in content

    assert registry.load_skill_content("nonexistent") is None





def test_skill_definition_to_dict():
    """SkillDefinition.to_dict() returns serializable dict."""
    sd = SkillDefinition(
        name="test", description="A test",
        content="# Content", filepath="/path/SKILL.md",
        license="MIT", compatibility="opencode",
        version="1.0.0",
        platforms=["linux", "macos"],
        tags=["spike", "prototype"],
        related_skills=["plan"],
        metadata={"audience": "devs"},
    )
    d = sd.to_dict()
    assert d["name"] == "test"
    assert d["description"] == "A test"
    assert d["license"] == "MIT"
    assert d["compatibility"] == "opencode"
    assert d["version"] == "1.0.0"
    assert d["platforms"] == ["linux", "macos"]
    assert d["tags"] == ["spike", "prototype"]
    assert d["related_skills"] == ["plan"]
    assert d["metadata"]["audience"] == "devs"


def test_discover_no_skills():
    """Discovering from empty directory returns empty list (extra_dirs only)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        skills = discover_skills(extra_dirs=[tmpdir])
        # May also find system skills, but at least our test dir returns nothing
        our_skills = [s for s in skills if tmpdir in s.filepath]
        assert our_skills == []


def test_skill_registry_list_skill_defs_dict():
    """list_skill_defs_dict() returns list of dicts."""
    registry = SkillRegistry()
    registry.add_skill_def(SkillDefinition(
        name="skill-a", description="Description A",
        content="Content A", filepath="/fake/a/SKILL.md",
    ))
    dicts = registry.list_skill_defs_dict()
    assert len(dicts) == 1
    assert dicts[0]["name"] == "skill-a"
    assert dicts[0]["description"] == "Description A"


def test_non_skill_dirs_ignored():
    """Directories without SKILL.md are ignored during discovery."""
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "not-a-skill"))
        os.makedirs(os.path.join(tmpdir, "empty-dir"))
        skills = discover_skills(extra_dirs=[tmpdir])
        our_empty = [s for s in skills if tmpdir in s.filepath]
        assert our_empty == []


def test_agent_auto_discover():
    """Agent auto-discovers skills on init unless disabled."""
    from agent_core import Agent
    from tests.test_basic import MockDatabase, MockLLM

    db = MockDatabase()
    llm = MockLLM()

    agent = Agent(db=db, llm=llm, auto_discover_skills=True)
    assert agent.skill_registry is not None
    # Should have found at least the system lark-* skills from ~/.agents/skills/
    assert len(agent.skill_registry.list_skill_defs()) > 0

    agent_no_disc = Agent(db=db, llm=llm, auto_discover_skills=False)
    assert len(agent_no_disc.skill_registry.list_skill_defs()) == 0


def test_agent_use_skill_in_loop():
    """Agent loads skill via use_skill tool, then finishes."""
    from agent_core import Agent

    db = MockDatabase()
    llm = ToolCallMockLLM([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="use_skill", arguments={"name": "test-skill"})]),
        LLMResponse(content="done with skill help"),
    ])
    agent = Agent(db=db, llm=llm, auto_discover_skills=False)

    agent.skill_registry.add_skill_def(SkillDefinition(
        name="test-skill", description="A test skill",
        content="## Instructions\nDo the thing carefully.",
        filepath="/fake/test-skill/SKILL.md",
    ))

    config = type("Config", (), {"base_url": "http://localhost", "sys_prompt": "You are a helper.", "api_spec": {}, "skill_extra_dirs": None, "verbose": True})()
    session_id = db.create_agent_session("test-skill")

    async def _collect_evs():
        _evs = []
        async for _ev in agent.chat_stream_events(
            user_message="help me with something",
            session_id=session_id,
            config=config,
            ):
            _evs.append(_ev)
        return _evs

    events = asyncio.run(_collect_evs())
    event_types = [e[0] for e in events]
    assert "skill_loaded" in event_types
    assert "done" in event_types
    print(f"  [OK] use_skill flow events: {event_types}")


def test_discover_does_not_crash_on_permission_error():
    """Silently skip directories without read permission."""
    with tempfile.TemporaryDirectory() as tmpdir:
        no_perm = os.path.join(tmpdir, "no-perm")
        os.makedirs(no_perm)
        try:
            os.chmod(no_perm, 0o000)
            skills = discover_skills(extra_dirs=[tmpdir])
            assert isinstance(skills, list)
        finally:
            os.chmod(no_perm, 0o755)


def test_shell_exec_simple():
    """Agent can execute shell commands via EXEC action."""
    from agent_core.engine.executor import _execute_shell_command

    step_out, ok, error = _execute_shell_command("echo hello world", step_num=1)
    assert ok
    assert step_out["exit_code"] == 0
    assert "hello world" in step_out.get("result_preview", "")


def test_shell_exec_fail():
    """Non-existent command returns error."""
    from agent_core.engine.executor import _execute_shell_command

    step_out, ok, error = _execute_shell_command("nonexistent_cmd_xyz", step_num=1)
    assert not ok
    assert error is not None


def test_shell_exec_in_agent_loop():
    """Agent executes shell via terminal tool, then finishes."""
    from agent_core import Agent

    db = MockDatabase()
    llm = ToolCallMockLLM([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="terminal", arguments={"command": "echo hello from agent"})]),
        LLMResponse(content="shell command executed"),
    ])
    agent = Agent(db=db, llm=llm, auto_discover_skills=False)

    config = type("Config", (), {"base_url": "http://localhost", "sys_prompt": "You are a helper.", "api_spec": {},         "skill_extra_dirs": None, "shell_timeout": 120, "shell_cwd": None, "verbose": True})()
    session_id = db.create_agent_session("test-shell")

    async def _collect_evs():
        _evs = []
        async for _ev in agent.chat_stream_events(
            user_message="run echo command",
            session_id=session_id,
            config=config,
            ):
            _evs.append(_ev)
        return _evs

    events = asyncio.run(_collect_evs())
    event_types = [e[0] for e in events]
    assert "step_start" in event_types
    assert "step_done" in event_types
    assert "done" in event_types
    print(f"  [OK] shell exec flow events: {event_types}")


# ---------------------------------------------------------------------------
# Tool-calling tests (Hermes-style function calling)
# ---------------------------------------------------------------------------


class ToolCallMockLLM(MockLLM):
    """MockLLM that returns tool_calls via chat() method."""
    def __init__(self, tool_calls_responses=None):
        super().__init__()
        self._tool_responses = tool_calls_responses or []
        self._call_index = 0

    def chat(self, messages, temperature=0.1, tools=None):
        if self._call_index < len(self._tool_responses):
            resp = self._tool_responses[self._call_index]
            self._call_index += 1
            return resp
        return LLMResponse(content='{"done": true, "reply": "fallback"}')


def test_tool_registry_basic():
    """ToolRegistry registers and dispatches tools."""
    registry = ToolRegistry()
    assert not registry.has_tools()

    def my_handler(args, ctx):
        return ToolResult(ok=True, data=args.get("value"))

    registry.register("my_tool", my_handler,
                      description="A test tool",
                      parameters={"type": "object", "properties": {"value": {"type": "string"}}})
    assert registry.has_tools()
    assert registry.list_tools() == ["my_tool"]

    result = registry.dispatch("my_tool", {"value": "hello"}, {})
    assert result.ok
    assert result.data == "hello"


def test_tool_unknown_name():
    """Dispatching unknown tool returns error ToolResult."""
    registry = ToolRegistry()
    result = registry.dispatch("nope", {}, {})
    assert not result.ok
    assert "未知" in result.error


def test_tool_text_only_is_final():
    """LLM returns text-only → agent treats it as final reply (Hermes-style done)."""
    db = MockDatabase()
    llm = ToolCallMockLLM([
        LLMResponse(content="The task is complete. Here are the results."),
    ])
    agent = Agent(db=db, llm=llm, auto_discover_skills=False)

    config = type("Config", (), {"base_url": "http://localhost", "sys_prompt": "You are a helper.", "api_spec": {},
                                 "shell_timeout": 120, "shell_cwd": None, "verbose": True})()
    session_id = db.create_agent_session("text-done")

    async def _collect_evs():
        _evs = []
        async for _ev in agent.chat_stream_events(
            user_message="do something",
            session_id=session_id,
            config=config,
            ):
            _evs.append(_ev)
        return _evs

    events = asyncio.run(_collect_evs())
    event_types = [e[0] for e in events]
    assert "done" in event_types
    done_data = [e[1] for e in events if e[0] == "done"][0]
    assert "here are the results" in done_data["reply"].lower()
    assert "text_stream" in event_types
    print(f"  [OK] text-only done flow events: {event_types}")


def test_tool_new_style_terminal():
    """LLM calls terminal tool → agent executes shell and yields step events."""
    db = MockDatabase()
    llm = ToolCallMockLLM([
        LLMResponse(
            content="I'll run a command",
            tool_calls=[ToolCall(id="call_1", name="terminal", arguments={"command": "echo 'tool exec'"})],
        ),
        LLMResponse(content='{"done": true, "reply": "done with shell"}'),
    ])
    agent = Agent(db=db, llm=llm, auto_discover_skills=False)

    config = type("Config", (), {"base_url": "http://localhost", "sys_prompt": "You are a helper.", "api_spec": {},
                                 "shell_timeout": 120, "shell_cwd": None, "verbose": True})()
    session_id = db.create_agent_session("tool-term")

    async def _collect_evs():
        _evs = []
        async for _ev in agent.chat_stream_events(
            user_message="run a command",
            session_id=session_id,
            config=config,
            ):
            _evs.append(_ev)
        return _evs

    events = asyncio.run(_collect_evs())
    event_types = [e[0] for e in events]
    assert "step_start" in event_types
    assert "step_done" in event_types
    assert "done" in event_types
    print(f"  [OK] tool-style terminal flow events: {event_types}")


def test_tool_new_style_use_skill():
    """LLM calls use_skill tool → agent loads skill and continues."""
    db = MockDatabase()
    llm = ToolCallMockLLM([
        LLMResponse(
            tool_calls=[ToolCall(id="call_1", name="use_skill", arguments={"name": "test-skill"})],
        ),
        LLMResponse(content='{"done": true, "reply": "done with skill"}'),
    ])
    agent = Agent(db=db, llm=llm, auto_discover_skills=False)
    agent.skill_registry.add_skill_def(SkillDefinition(
        name="test-skill", description="A test skill",
        content="## Instructions\nDo the thing.",
        filepath="/fake/test-skill/SKILL.md",
    ))

    config = type("Config", (), {"base_url": "http://localhost", "sys_prompt": "You are a helper.", "api_spec": {},
                                 "shell_timeout": 120, "shell_cwd": None, "verbose": True})()
    session_id = db.create_agent_session("tool-skill")

    async def _collect_evs():
        _evs = []
        async for _ev in agent.chat_stream_events(
            user_message="use a skill",
            session_id=session_id,
            config=config,
            ):
            _evs.append(_ev)
        return _evs

    events = asyncio.run(_collect_evs())
    event_types = [e[0] for e in events]
    assert "skill_loaded" in event_types
    assert "done" in event_types
    print(f"  [OK] tool-style use_skill flow events: {event_types}")


def test_tool_use_skill_template_substitution():
    """use_skill substitutes ${HERMES_SKILL_DIR} in skill content."""
    db = MockDatabase()
    llm = ToolCallMockLLM([
        LLMResponse(
            tool_calls=[ToolCall(id="c1", name="use_skill", arguments={"name": "templated"})],
        ),
        LLMResponse(content='{"done": true, "reply": "done"}'),
    ])
    agent = Agent(db=db, llm=llm, auto_discover_skills=False)
    agent.skill_registry.add_skill_def(SkillDefinition(
        name="templated", description="Has template vars",
        content="Run from ${HERMES_SKILL_DIR}",
        filepath="C:\\skills\\templated\\SKILL.md",
    ))

    config = type("Config", (), {"base_url": "http://localhost", "sys_prompt": "helper", "api_spec": {},
                                 "shell_timeout": 120, "shell_cwd": None, "verbose": True})()
    session_id = db.create_agent_session("template-test")

    async def _collect_evs():
        _evs = []
        async for _ev in agent.chat_stream_events(
            user_message="use templated skill", session_id=session_id, config=config,
            ):
            _evs.append(_ev)
        return _evs

    events = asyncio.run(_collect_evs())
    # Find the tool result — it should contain the substituted path
    tool_results = [e[1] for e in events if e[0] == "tool_result"]
    assert len(tool_results) >= 1, f"No tool_result in events: {[e[0] for e in events]}"
    assert "C:\\skills\\templated" in tool_results[0].get("summary", "")
    print(f"  [OK] template substitution: {tool_results[0].get('summary', '')[:80]}")


def test_tool_new_style_skills_list():
    """LLM calls skills_list tool → agent returns skill names."""
    db = MockDatabase()
    llm = ToolCallMockLLM([
        LLMResponse(
            tool_calls=[ToolCall(id="call_1", name="skills_list", arguments={})],
        ),
        LLMResponse(content='{"done": true, "reply": "done listing"}'),
    ])
    agent = Agent(db=db, llm=llm, auto_discover_skills=False)
    agent.skill_registry.add_skill_def(SkillDefinition(
        name="test-skill", description="A test skill",
        content="## Instructions", filepath="/fake/SKILL.md",
    ))

    config = type("Config", (), {"base_url": "http://localhost", "sys_prompt": "You are a helper.", "api_spec": {},
                                 "shell_timeout": 120, "shell_cwd": None, "verbose": True})()
    session_id = db.create_agent_session("tool-list")

    async def _collect_evs():
        _evs = []
        async for _ev in agent.chat_stream_events(
            user_message="list skills",
            session_id=session_id,
            config=config,
            ):
            _evs.append(_ev)
        return _evs

    events = asyncio.run(_collect_evs())
    event_types = [e[0] for e in events]
    assert "done" in event_types
    print(f"  [OK] tool-style skills_list flow events: {event_types}")


def test_tool_auto_register_default():
    """Agent auto-registers built-in tools by default."""
    db = MockDatabase()
    llm = ToolCallMockLLM()
    agent = Agent(db=db, llm=llm, auto_discover_skills=False)
    tools = agent.tool_registry.list_tools()
    assert "delegate_task" in tools
    assert "terminal" in tools
    assert "terminal" in tools
    assert "use_skill" in tools
    assert "skills_list" in tools
    assert "ask_user" in tools
    print(f"  [OK] auto-registered tools: {tools}")





def test_tool_mixed_use_skill_and_tool():
    """LLM uses use_skill text JSON, then terminal tool in next round."""

    db = MockDatabase()

    class MixedLLM(MockLLM):
        def __init__(self):
            self.call_count = 0
        def chat(self, messages, temperature=0.1, tools=None):
            if self.call_count == 0:
                self.call_count += 1
                # tool call: load a skill
                return LLMResponse(
                    tool_calls=[ToolCall(id="c1", name="use_skill", arguments={"name": "test-skill"})],
                )
            elif self.call_count == 1:
                self.call_count += 1
                # tool call: run terminal
                return LLMResponse(
                    content="Now running terminal",
                    tool_calls=[ToolCall(id="c2", name="terminal", arguments={"command": "echo mixed"})],
                )
            else:
                return LLMResponse(content="all done")

    llm = MixedLLM()
    agent = Agent(db=db, llm=llm, auto_discover_skills=False)
    agent.skill_registry.add_skill_def(SkillDefinition(
        name="test-skill", description="A test skill",
        content="## Instructions\nDo the thing.",
        filepath="/fake/test-skill/SKILL.md",
    ))

    config = type("Config", (), {"base_url": "http://localhost", "sys_prompt": "You are a helper.", "api_spec": {},
                                 "shell_timeout": 120, "shell_cwd": None, "verbose": True})()
    session_id = db.create_agent_session("mixed")

    async def _collect_evs():
        _evs = []
        async for _ev in agent.chat_stream_events(
            user_message="do mixed things",
            session_id=session_id,
            config=config,
            ):
            _evs.append(_ev)
        return _evs

    events = asyncio.run(_collect_evs())
    event_types = [e[0] for e in events]
    assert "skill_loaded" in event_types
    assert "step_start" in event_types
    assert "step_done" in event_types
    assert "done" in event_types
    print(f"  [OK] mixed use_skill+terminal flow events: {event_types}")
