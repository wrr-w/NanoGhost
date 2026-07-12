# Official MCP Client Replacement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace NanoGhost custom MCP SSE/stdio transport clients with thin adapters over the official Python `mcp` client library while preserving the current MCP manager and folded tool model.

**Architecture:** Keep `MCPManager`, config parsing, awareness summary, and folded `mcp_<server_id>` tools unchanged at the conceptual level. Replace only `http_sse.py` and `stdio_client.py` with official-client-backed adapters that preserve the existing synchronous method contract used by the rest of NanoGhost.

**Tech Stack:** Python, official `mcp` Python client library, existing MCPManager/ToolRegistry architecture, pytest, PyInstaller

---

## File Structure Map

### New Files

- `tests/test_mcp_official_clients.py`
  - Focused tests for the new official-client-backed adapters and sync wrapper behavior

### Modified Files

- `src/agent_core/mcp/http_sse.py`
  - Replace custom SSE protocol implementation with official `mcp` client adapter
- `src/agent_core/mcp/stdio_client.py`
  - Replace custom stdio protocol implementation with official `mcp` client adapter
- `src/agent_core/mcp/manager.py`
  - Keep overall behavior, only normalize response shape if needed
- `src/agent_core/cli.py`
  - Ensure `probe/tools/reload` use transport-correct clients consistently
- `requirements.txt`
  - Add official `mcp` dependency
- `pyproject.toml`
  - Add official `mcp` dependency
- `build.spec`
  - Add hidden imports / packaging support required by official `mcp` dependency

### Files Explicitly Not Restructured

- `src/agent_core/mcp/config.py`
  - Keep transport/config parsing model as-is
- `src/agent_core/engine/agent.py`
  - Keep awareness injection and schema gating model unchanged
- `src/agent_core/tool/registry.py`
  - Keep tool registration model unchanged

---

### Task 1: Add Official MCP Dependency and Packaging Support

**Files:**
- Modify: `requirements.txt`
- Modify: `pyproject.toml`
- Modify: `build.spec`

- [ ] **Step 1: Write the failing dependency/import check**

```bash
python -c "import mcp; print(mcp.__file__)"
```

Expected:

```text
ModuleNotFoundError: No module named 'mcp'
```

- [ ] **Step 2: Add the official MCP dependency to `requirements.txt`**

```text
json5>=0.9
openai>=1.0
requests>=2.0
python-dotenv>=1.0
lark-oapi>=1.6.0
fastembed>=0.5
mcp>=1.0
```

- [ ] **Step 3: Add the official MCP dependency to `pyproject.toml`**

```toml
[project]
name = "agent-core"
version = "0.1.0"
description = "Standalone LLM-driven Agent core: decision loop, memory system, Feishu channel"
requires-python = ">=3.10"
license = {text = "MIT"}
dependencies = [
    "json5>=0.9",
    "openai>=1.0",
    "requests>=2.0",
    "python-dotenv>=1.0",
    "fastembed>=0.5",
    "mcp>=1.0",
]
```

- [ ] **Step 4: Update `build.spec` hidden imports for official MCP packaging**

```python
hiddenimports=[
    "json5",
    "openai",
    "requests",
    "dotenv",
    "lark_oapi",
    "fastembed",
    "typing_extensions",
    "anyio",
    "httpx",
    "pydantic",
    "pydantic_core",
    "certifi",
    "sqlite3",
    "_sqlite3",
    "pydantic_core._pydantic_core",
    "jiter",
    "jiter.jiter",
    "mmh3",
    "py_rust_stemmers",
    "py_rust_stemmers.py_rust_stemmers",
    "tokenizers",
    "tokenizers.tokenizers",
    "yaml",
    "yaml._yaml",
    "hf_xet",
    "hf_xet.hf_xet",
    "google._upb._message",
    "mcp",
    "mcp.client",
    "mcp.client.sse",
    "mcp.client.stdio",
    "mcp.types",
]
```

- [ ] **Step 5: Install dependencies and verify import succeeds**

Run:

```bash
pip install -r requirements.txt
python -c "import mcp; print('mcp import ok')"
```

Expected:

```text
mcp import ok
```

- [ ] **Step 6: Commit**

```bash
git add requirements.txt pyproject.toml build.spec
git commit -m "build: add official mcp dependency"
```

---

### Task 2: Replace HTTP/SSE Transport with Official MCP Client Adapter

**Files:**
- Modify: `src/agent_core/mcp/http_sse.py`
- Test: `tests/test_mcp_official_clients.py`

- [ ] **Step 1: Write the failing tests for the new HTTP/SSE adapter contract**

```python
from agent_core.mcp.config import MCPServerConfig
from agent_core.mcp.http_sse import MCPHttpSSEClient


def test_http_sse_client_exposes_existing_sync_contract():
    cfg = MCPServerConfig(
        server_id="iwms",
        transport="sse",
        url="http://127.0.0.1:8001/mcp/sse",
        headers={},
        timeout_seconds=5,
    )
    client = MCPHttpSSEClient(cfg)

    assert hasattr(client, "probe")
    assert hasattr(client, "list_tools")
    assert hasattr(client, "call_tool")
```

- [ ] **Step 2: Run the focused test to verify the current file is still under transition**

Run:

```bash
python -m pytest tests/test_mcp_official_clients.py::test_http_sse_client_exposes_existing_sync_contract -q
```

Expected:

```text
FAIL or ERROR until the test file exists and the adapter path is stabilized
```

- [ ] **Step 3: Replace the custom SSE implementation with an official-client-backed adapter**

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from mcp import ClientSession
from mcp.client.sse import sse_client

from .config import MCPServerConfig


@dataclass
class MCPProbeResult:
    ok: bool
    status: str
    error: Optional[str] = None
    duration_ms: int = 0


class MCPHttpSSEClient:
    def __init__(self, cfg: MCPServerConfig):
        self.cfg = cfg

    async def _run_session(self, op):
        headers = self.cfg.headers or {}
        timeout = max(1, int(self.cfg.timeout_seconds))
        async with sse_client(self.cfg.url, headers=headers, timeout=timeout) as streams:
            read_stream, write_stream = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await op(session)

    def _run(self, coro):
        return asyncio.run(coro)
```

- [ ] **Step 4: Implement `probe`, `list_tools`, and `call_tool` using the official session**

```python
    def probe(self) -> MCPProbeResult:
        t0 = time.time()
        try:
            self._run(self._run_session(lambda session: asyncio.sleep(0, result=True)))
            return MCPProbeResult(ok=True, status="connected", duration_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            return MCPProbeResult(ok=False, status="unreachable", error=str(e), duration_ms=int((time.time() - t0) * 1000))

    def list_tools(self) -> Tuple[bool, Any, Optional[str], int]:
        t0 = time.time()
        try:
            result = self._run(self._run_session(lambda session: session.list_tools()))
            payload = result.model_dump() if hasattr(result, "model_dump") else result
            return True, payload, None, int((time.time() - t0) * 1000)
        except Exception as e:
            return False, None, str(e), int((time.time() - t0) * 1000)

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Tuple[bool, Any, Optional[str], int]:
        t0 = time.time()
        try:
            result = self._run(self._run_session(lambda session: session.call_tool(name, arguments or {})))
            payload = result.model_dump() if hasattr(result, "model_dump") else result
            return True, payload, None, int((time.time() - t0) * 1000)
        except Exception as e:
            return False, None, str(e), int((time.time() - t0) * 1000)
```

- [ ] **Step 5: Add a focused contract test file**

```python
from agent_core.mcp.config import MCPServerConfig
from agent_core.mcp.http_sse import MCPHttpSSEClient
from agent_core.mcp.stdio_client import MCPStdioClient


def _sse_cfg():
    return MCPServerConfig(
        server_id="iwms",
        transport="sse",
        url="http://127.0.0.1:8001/mcp/sse",
        headers={},
        timeout_seconds=5,
    )


def _stdio_cfg():
    return MCPServerConfig(
        server_id="capture",
        transport="stdio",
        url="python",
        headers={},
        timeout_seconds=5,
        extra_args=["-V"],
    )


def test_http_sse_client_exposes_existing_sync_contract():
    client = MCPHttpSSEClient(_sse_cfg())
    assert hasattr(client, "probe")
    assert hasattr(client, "list_tools")
    assert hasattr(client, "call_tool")


def test_stdio_client_exposes_existing_sync_contract():
    client = MCPStdioClient(_stdio_cfg())
    assert hasattr(client, "probe")
    assert hasattr(client, "list_tools")
    assert hasattr(client, "call_tool")
```

- [ ] **Step 6: Run the HTTP/SSE-focused tests**

Run:

```bash
python -m pytest tests/test_mcp_official_clients.py -q
```

Expected:

```text
all contract tests pass
```

- [ ] **Step 7: Commit**

```bash
git add src/agent_core/mcp/http_sse.py tests/test_mcp_official_clients.py
git commit -m "feat: replace MCP SSE client with official adapter"
```

---

### Task 3: Replace stdio Transport with Official MCP Client Adapter

**Files:**
- Modify: `src/agent_core/mcp/stdio_client.py`
- Test: `tests/test_mcp_official_clients.py`

- [ ] **Step 1: Write the failing stdio adapter behavior test**

```python
def test_stdio_client_probe_returns_result_object():
    cfg = _stdio_cfg()
    client = MCPStdioClient(cfg)
    result = client.probe()
    assert hasattr(result, "ok")
    assert hasattr(result, "status")
```

- [ ] **Step 2: Run the stdio-focused test**

Run:

```bash
python -m pytest tests/test_mcp_official_clients.py::test_stdio_client_probe_returns_result_object -q
```

Expected:

```text
FAIL until stdio adapter is rewritten and stable
```

- [ ] **Step 3: Replace the custom stdio implementation with an official-client-backed adapter**

```python
import asyncio
import time
from typing import Any, Dict, Optional, Tuple

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from .config import MCPServerConfig
from .http_sse import MCPProbeResult


class MCPStdioClient:
    def __init__(self, cfg: MCPServerConfig):
        self.cfg = cfg

    async def _run_session(self, op):
        params = StdioServerParameters(
            command=self.cfg.url,
            args=list(self.cfg.extra_args or []),
            env=None,
        )
        async with stdio_client(params) as streams:
            read_stream, write_stream = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await op(session)

    def _run(self, coro):
        return asyncio.run(coro)
```

- [ ] **Step 4: Implement `probe`, `list_tools`, and `call_tool` with the same outward contract**

```python
    def probe(self):
        t0 = time.time()
        try:
            self._run(self._run_session(lambda session: asyncio.sleep(0, result=True)))
            return MCPProbeResult(ok=True, status="connected", duration_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            return MCPProbeResult(ok=False, status="unreachable", error=str(e), duration_ms=int((time.time() - t0) * 1000))

    def list_tools(self) -> Tuple[bool, Any, Optional[str], int]:
        t0 = time.time()
        try:
            result = self._run(self._run_session(lambda session: session.list_tools()))
            payload = result.model_dump() if hasattr(result, "model_dump") else result
            return True, payload, None, int((time.time() - t0) * 1000)
        except Exception as e:
            return False, None, str(e), int((time.time() - t0) * 1000)

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Tuple[bool, Any, Optional[str], int]:
        t0 = time.time()
        try:
            result = self._run(self._run_session(lambda session: session.call_tool(name, arguments or {})))
            payload = result.model_dump() if hasattr(result, "model_dump") else result
            return True, payload, None, int((time.time() - t0) * 1000)
        except Exception as e:
            return False, None, str(e), int((time.time() - t0) * 1000)
```

- [ ] **Step 5: Run the focused adapter tests**

Run:

```bash
python -m pytest tests/test_mcp_official_clients.py -q
```

Expected:

```text
all adapter contract tests pass
```

- [ ] **Step 6: Commit**

```bash
git add src/agent_core/mcp/stdio_client.py tests/test_mcp_official_clients.py
git commit -m "feat: replace MCP stdio client with official adapter"
```

---

### Task 4: Normalize Official Client Results for Existing Manager Logic

**Files:**
- Modify: `src/agent_core/mcp/manager.py`
- Test: `tests/test_mcp_awareness.py`

- [ ] **Step 1: Write the failing normalization test for manager extraction**

```python
from agent_core.mcp.manager import _extract_tools


def test_extract_tools_accepts_official_list_tools_payload():
    payload = {
        "tools": [
            {"name": "foo", "description": "Foo tool", "inputSchema": {"type": "object"}},
            {"name": "bar", "description": "Bar tool", "inputSchema": {"type": "object"}},
        ]
    }
    tools = _extract_tools(payload)
    assert set(tools.keys()) == {"foo", "bar"}
```

- [ ] **Step 2: Run the focused manager test**

Run:

```bash
python -m pytest tests/test_mcp_awareness.py::test_extract_tools_accepts_official_list_tools_payload -q
```

Expected:

```text
PASS or FAIL depending on official result normalization needs
```

- [ ] **Step 3: Normalize response payloads at the manager boundary if needed**

```python
def _normalize_mcp_result(result_obj: Any) -> Any:
    if hasattr(result_obj, "model_dump"):
        return result_obj.model_dump()
    return result_obj


def _extract_tools(result_obj: Any) -> Dict[str, Dict[str, Any]]:
    result_obj = _normalize_mcp_result(result_obj)
    if not isinstance(result_obj, dict):
        return {}
    tools = result_obj.get("tools")
    if not isinstance(tools, list):
        return {}
    out = {}
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        out[name] = t
    return out
```

- [ ] **Step 4: Run the MCP awareness test suite**

Run:

```bash
python -m pytest tests/test_mcp_awareness.py -q
```

Expected:

```text
all MCP awareness tests pass
```

- [ ] **Step 5: Commit**

```bash
git add src/agent_core/mcp/manager.py tests/test_mcp_awareness.py
git commit -m "refactor: normalize official MCP client payloads"
```

---

### Task 5: Fix CLI MCP Commands to Use Transport-correct Official Adapters

**Files:**
- Modify: `src/agent_core/cli.py`

- [ ] **Step 1: Write the failing expectation check for CLI transport handling**

```python
def _client_for(cfg):
    from agent_core.mcp.http_sse import MCPHttpSSEClient
    from agent_core.mcp.stdio_client import MCPStdioClient
    return MCPStdioClient(cfg) if cfg.transport == "stdio" else MCPHttpSSEClient(cfg)
```

- [ ] **Step 2: Replace SSE-only assumptions in `mcp tools` and `mcp reload`**

```python
def _cmd_mcp_tools(args) -> int:
    inst = _instance_dir_from_args(args)
    servers = {s.server_id: s for s in resolve_servers(inst)}
    s = servers.get(args.server_id)
    if not s:
        raise SystemExit(f"server 不可用或不在白名单中: {args.server_id}")
    from agent_core.mcp.stdio_client import MCPStdioClient
    client = MCPStdioClient(s) if s.transport == "stdio" else MCPHttpSSEClient(s)
    ok, result, err, dur = client.list_tools()
    tools = result.get("tools") if isinstance(result, dict) else None
```

```python
def _cmd_mcp_reload(args) -> int:
    inst = _instance_dir_from_args(args)
    servers = resolve_servers(inst)
    results = []
    for s in servers:
        from agent_core.mcp.stdio_client import MCPStdioClient
        client = MCPStdioClient(s) if s.transport == "stdio" else MCPHttpSSEClient(s)
        pr = client.probe()
        ok, tool_res, err, dur = client.list_tools() if pr.ok else (False, None, pr.error, pr.duration_ms)
```

- [ ] **Step 3: Run CLI MCP checks**

Run:

```bash
python -c "from pathlib import Path; from agent_core.cli import _instance_dir_from_args, _cmd_mcp_probe; print('cli import ok')"
```

Expected:

```text
cli import ok
```

- [ ] **Step 4: Commit**

```bash
git add src/agent_core/cli.py
git commit -m "fix: use transport-aware MCP clients in CLI commands"
```

---

### Task 6: Runtime Verification Against IWMS and Capture

**Files:**
- Modify: none required unless verification reveals a small fix

- [ ] **Step 1: Verify IWMS list_tools through the new official SSE adapter**

Run:

```bash
python -c "from pathlib import Path; from agent_core.mcp.config import resolve_servers; from agent_core.mcp.http_sse import MCPHttpSSEClient; inst=Path(r'C:\Users\Administrator\.nanoghost\instances\iwms'); s=resolve_servers(inst)[0]; c=MCPHttpSSEClient(s); print(c.probe()); print(c.list_tools())"
```

Expected:

```text
probe connected
list_tools returns a non-empty tools payload
```

- [ ] **Step 2: Verify Capture list_tools through the new official stdio adapter**

Run:

```bash
python -c "from pathlib import Path; from agent_core.mcp.config import resolve_servers; from agent_core.mcp.stdio_client import MCPStdioClient; inst=Path(r'C:\Users\Administrator\.nanoghost\instances\cc'); s=resolve_servers(inst)[0]; c=MCPStdioClient(s); print(c.probe()); print(c.list_tools())"
```

Expected:

```text
probe connected
list_tools returns capture tools
```

- [ ] **Step 3: Verify folded schemas appear for both instances**

Run:

```bash
python -c "import os,time; os.environ['INSTANCE_DIR']=r'C:\Users\Administrator\.nanoghost\instances\iwms'; from dotenv import load_dotenv; load_dotenv(r'C:\Users\Administrator\.nanoghost\instances\iwms\.env', override=True); from agent_core import Agent; from agent_core.adapters import SqliteDatabase, OpenAILLM, SqliteImagePort; db=SqliteDatabase(); a=Agent(db=db,llm=OpenAILLM(),image_port=SqliteImagePort(db),namespace='iwms-verify'); time.sleep(3); print([x['function']['name'] for x in a.tool_registry.get_available_schemas() if 'mcp' in x['function']['name']])"
```

Expected:

```text
['mcp_iwms']
```

- [ ] **Step 4: Run focused regression suite**

Run:

```bash
python -m pytest tests/test_mcp_official_clients.py tests/test_mcp_awareness.py tests/test_prompt_layering.py tests/test_memory_files.py -q
```

Expected:

```text
all targeted tests pass
```

- [ ] **Step 5: Prepare final progress/current-state handoff**

```text
已完成：
- 官方 mcp client 替换了 NanoGhost 自写 SSE/stdio 传输层
- iwms 与 capture 均通过官方 transport 接入
- 上层 MCP 管理、折叠工具、awareness summary 保持不变

当前现状：
- iwms 应可注册为 mcp_iwms
- capture 应可继续注册为 mcp_capture
- CLI 与运行时对 MCP transport 的处理保持一致
```

- [ ] **Step 6: Commit**

```bash
git add src/agent_core/mcp/http_sse.py src/agent_core/mcp/stdio_client.py src/agent_core/mcp/manager.py src/agent_core/cli.py tests/test_mcp_official_clients.py
git commit -m "feat: replace custom MCP transports with official clients"
```

---

## Self-Review

### Spec Coverage

- Replace HTTP/SSE transport: covered by Task 2
- Replace stdio transport: covered by Task 3
- Preserve manager model: covered by Task 4
- Keep CLI behavior correct across transports: covered by Task 5
- Verify IWMS and Capture both work: covered by Task 6
- Packaging/dependency impact: covered by Task 1

### Placeholder Scan

- No `TODO`, `TBD`, or filler steps remain
- Every code step contains concrete code
- Every verification step contains exact commands and expected outcomes

### Type Consistency

- The sync outer contract remains `probe/list_tools/call_tool`
- Official client responses are normalized at adapter or manager boundary
- The same transport-specific adapter selection rule is used across manager and CLI

---

**Plan complete and saved to `docs/superpowers/plans/2026-06-11-official-mcp-client-replacement.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
