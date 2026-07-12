# Capture SSE Manifest Unification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `capture` from stdio MCP to service-style SSE MCP and add a unified manifest/status/executable-tool model in NanoGhost so `capture` and `iwms` behave the same to the agent.

**Architecture:** The Capture repository exposes official SSE MCP endpoints backed by its existing API-spec-driven tool generation. NanoGhost treats both `capture` and `iwms` as SSE MCP services, persists a lightweight manifest cache per server, separates runtime status from executable tool registration, and keeps the folded `mcp_<server_id>` tool model.

**Tech Stack:** Python, official `mcp` library, FastAPI/ASGI SSE transport, NanoGhost MCPManager/ToolRegistry, pytest, YAML config, JSON cache files

---

## File Structure Map

### Capture Repository

- Modify: `E:\OperationsAssistantORIG\Tech\Code\Capture\server\mcp\capture_mcp_server.py`
  - Replace stdio-only entrypoint with reusable server definition plus SSE transport wiring
- Create: `E:\OperationsAssistantORIG\Tech\Code\Capture\server\mcp\capture_mcp_manifest.py`
  - Extract tool-definition building and handler wiring into a reusable module
- Modify: `E:\OperationsAssistantORIG\Tech\Code\Capture\server\app.py`
  - Mount `/mcp/sse` and `/mcp/messages` endpoints that expose the Capture MCP server over SSE
- Create: `E:\OperationsAssistantORIG\Tech\Code\Capture\tests\test_capture_mcp_sse.py`
  - Verify Capture exposes SSE MCP endpoints and tool discovery still works

### NanoGhost Repository

- Modify: `src/agent_core/mcp/config.py`
  - Support optional manifest metadata persistence fields and treat `capture` as SSE in examples/tests
- Create: `src/agent_core/mcp/manifest_cache.py`
  - Read/write local MCP manifest cache files
- Modify: `src/agent_core/mcp/manager.py`
  - Load and persist manifest cache, add richer status states, gate executable registration on `ready`
- Modify: `src/agent_core/engine/agent.py`
  - No architectural redesign; continue injecting awareness from manager output
- Modify: `src/agent_core/cli.py`
  - Add `mcp manifest` diagnostic and keep `probe/tools/reload` aligned with manifest/status model
- Modify: `tests/test_mcp_awareness.py`
  - Cover manifest cache, richer statuses, and awareness rendering
- Create: `tests/test_mcp_manifest_cache.py`
  - Focused tests for manifest cache persistence
- Modify: `tests/test_mcp_official_clients.py`
  - Add SSE-based Capture verification assumptions and preserve folded tool contract
- Modify: `build.spec`
  - Update packaging data if new cache/docs/runtime hooks are needed for diagnostics
- Modify: `docs/usage.md`
  - Document `capture` as SSE MCP and explain `known/loading/ready/stale/error`

### Files Explicitly Not Restructured

- `src/agent_core/tool/registry.py`
  - Keep current folded tool exposure model
- `src/agent_core/mcp/http_sse.py`
  - Reuse the official SSE client adapter already in place
- `src/agent_core/mcp/stdio_client.py`
  - Keep for other future/local cases, but `capture` no longer depends on it

---

### Task 1: Extract Capture MCP Definition and Expose SSE Endpoints

**Files:**
- Create: `E:\OperationsAssistantORIG\Tech\Code\Capture\server\mcp\capture_mcp_manifest.py`
- Modify: `E:\OperationsAssistantORIG\Tech\Code\Capture\server\mcp\capture_mcp_server.py`
- Modify: `E:\OperationsAssistantORIG\Tech\Code\Capture\server\app.py`
- Test: `E:\OperationsAssistantORIG\Tech\Code\Capture\tests\test_capture_mcp_sse.py`

- [ ] **Step 1: Write the failing SSE exposure test**

```python
from fastapi.testclient import TestClient

from server.app import app


def test_capture_exposes_mcp_sse_endpoint():
    client = TestClient(app)
    response = client.get("/mcp/sse")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
```

- [ ] **Step 2: Run the test to confirm `/mcp/sse` does not exist yet**

Run:

```bash
cd /d E:\OperationsAssistantORIG\Tech\Code\Capture
python -m pytest tests/test_capture_mcp_sse.py::test_capture_exposes_mcp_sse_endpoint -q
```

Expected:

```text
FAIL
E       assert 404 == 200
```

- [ ] **Step 3: Extract reusable tool/handler construction into `capture_mcp_manifest.py`**

```python
import json
import os
import sys
from typing import Callable

import httpx
from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, TextContent, Tool


def build_capture_server(api_spec_path: str, capture_base_url: str) -> Server:
    spec = load_api_spec(api_spec_path)
    tool_handlers = build_tool_handlers(spec, capture_base_url)
    tools_list = build_tools_list(spec)

    server = Server("capture-mcp")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return tools_list

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "capture_api_call":
            result = build_generic_handler(capture_base_url)(arguments)
        elif name in tool_handlers:
            result = tool_handlers[name](arguments)
        else:
            err = json.dumps({"ok": False, "error": f"未知 tool: {name}"}, ensure_ascii=False)
            result = CallToolResult(content=[TextContent(type="text", text=err)])
        return result.content

    return server
```

- [ ] **Step 4: Rewrite `capture_mcp_server.py` into a thin entrypoint**

```python
import anyio
from mcp.server.stdio import stdio_server

from server.mcp.capture_mcp_manifest import (
    API_SPEC_PATH,
    CAPTURE_BASE_URL,
    build_capture_server,
)


server = build_capture_server(API_SPEC_PATH, CAPTURE_BASE_URL)


if __name__ == "__main__":
    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(main)
```

- [ ] **Step 5: Mount official SSE transport in `server/app.py`**

```python
from fastapi import Request
from mcp.server.sse import SseServerTransport

from server.mcp.capture_mcp_manifest import API_SPEC_PATH, CAPTURE_BASE_URL, build_capture_server


capture_mcp_server = build_capture_server(API_SPEC_PATH, CAPTURE_BASE_URL)
capture_mcp_sse = SseServerTransport("/mcp/messages")


@app.get("/mcp/sse")
async def capture_mcp_sse_endpoint(request: Request):
    async with capture_mcp_sse.connect_sse(request.scope, request.receive, request._send) as (
        read_stream,
        write_stream,
    ):
        await capture_mcp_server.run(
            read_stream,
            write_stream,
            capture_mcp_server.create_initialization_options(),
        )


@app.post("/mcp/messages")
async def capture_mcp_messages_endpoint(request: Request):
    await capture_mcp_sse.handle_post_message(request.scope, request.receive, request._send)
```

- [ ] **Step 6: Add a tool-discovery test that proves Capture still exposes tools over SSE**

```python
from pathlib import Path

from agent_core.mcp.http_sse import MCPHttpSSEClient
from agent_core.mcp.config import MCPServerConfig


def test_capture_sse_list_tools_returns_capture_tools():
    cfg = MCPServerConfig(
        server_id="capture",
        transport="sse",
        url="http://127.0.0.1:8000/mcp/sse",
        headers={},
        timeout_seconds=5,
    )
    client = MCPHttpSSEClient(cfg)
    ok, payload, err, _ = client.list_tools()

    assert ok is True
    assert err is None
    assert any(t["name"] == "capture_api_call" for t in payload["tools"])
```

- [ ] **Step 7: Run the Capture SSE test suite**

Run:

```bash
cd /d E:\OperationsAssistantORIG\Tech\Code\Capture
python -m pytest tests/test_capture_mcp_sse.py -q
```

Expected:

```text
2 passed
```

- [ ] **Step 8: Commit the Capture-side SSE exposure**

```bash
cd /d E:\OperationsAssistantORIG\Tech\Code\Capture
git add server/mcp/capture_mcp_manifest.py server/mcp/capture_mcp_server.py server/app.py tests/test_capture_mcp_sse.py
git commit -m "feat: expose capture MCP over SSE"
```

---

### Task 2: Switch NanoGhost Capture Config from stdio to SSE

**Files:**
- Modify: `C:\Users\Administrator\.nanoghost\config.yaml`
- Modify: `src/agent_core/mcp/config.py`
- Test: `tests/test_mcp_awareness.py`

- [ ] **Step 1: Write the failing config-resolution test for SSE capture**

```python
from pathlib import Path

from agent_core.mcp.config import resolve_servers


def test_resolve_servers_reads_capture_as_sse(tmp_path: Path, monkeypatch):
    global_cfg = tmp_path / "global.yaml"
    global_cfg.write_text(
        "mcp_servers:\n"
        "  capture:\n"
        "    transport: sse\n"
        "    url: http://127.0.0.1:8000/mcp/sse\n",
        encoding="utf-8",
    )
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / "config.yaml").write_text("mcp:\n  enabled_only:\n    - capture\n", encoding="utf-8")
    monkeypatch.setattr("agent_core.mcp.config._default_global_config_path", lambda: global_cfg)

    servers = resolve_servers(inst)

    assert len(servers) == 1
    assert servers[0].server_id == "capture"
    assert servers[0].transport == "sse"
```

- [ ] **Step 2: Run the focused config test**

Run:

```bash
cd /d E:\OperationsAssistantORIG\Tech\Code\NanoGhost
python -m pytest tests/test_mcp_awareness.py::test_resolve_servers_reads_capture_as_sse -q
```

Expected:

```text
FAIL until test is added
```

- [ ] **Step 3: Update the user config to point Capture at SSE**

```yaml
mcp_servers:
  capture:
    transport: sse
    url: http://127.0.0.1:8000/mcp/sse
  iwms:
    transport: sse
    url: http://127.0.0.1:8001/mcp/sse
```

- [ ] **Step 4: Keep `config.py` transport parsing generic and add metadata defaults used by manifest cache**

```python
return MCPServerConfig(
    server_id=server_id,
    transport=str(raw.get("transport") or "sse").strip().lower(),
    url=str(raw.get("url") or raw.get("command") or "").strip(),
    headers=raw.get("headers") or {},
    timeout_seconds=int(raw.get("timeout_seconds") or 30),
    extra_args=list(raw.get("args") or []),
    title=str(raw.get("title") or server_id),
    description=str(raw.get("description") or ""),
    use_cases=list(raw.get("use_cases") or []),
    notes=str(raw.get("notes") or ""),
)
```

- [ ] **Step 5: Run the awareness/config tests**

Run:

```bash
python -m pytest tests/test_mcp_awareness.py::test_resolve_servers_reads_capture_as_sse -q
```

Expected:

```text
1 passed
```

- [ ] **Step 6: Commit the config migration**

```bash
cd /d E:\OperationsAssistantORIG\Tech\Code\NanoGhost
git add src/agent_core/mcp/config.py
git commit -m "config: switch capture MCP to SSE"
```

---

### Task 3: Add Manifest Cache Persistence in NanoGhost

**Files:**
- Create: `src/agent_core/mcp/manifest_cache.py`
- Test: `tests/test_mcp_manifest_cache.py`

- [ ] **Step 1: Write the failing manifest-cache roundtrip test**

```python
from pathlib import Path

from agent_core.mcp.manifest_cache import load_manifest_cache, save_manifest_cache


def test_manifest_cache_roundtrip(tmp_path: Path):
    save_manifest_cache(
        tmp_path,
        "capture",
        {
            "server_id": "capture",
            "title": "capture",
            "actions": [{"name": "capture_api_call", "description": "generic fallback"}],
            "last_manifest_refresh_at": "2026-07-06T12:00:00Z",
        },
    )

    payload = load_manifest_cache(tmp_path, "capture")

    assert payload["server_id"] == "capture"
    assert payload["actions"][0]["name"] == "capture_api_call"
```

- [ ] **Step 2: Run the focused manifest-cache test**

Run:

```bash
python -m pytest tests/test_mcp_manifest_cache.py::test_manifest_cache_roundtrip -q
```

Expected:

```text
FAIL with ModuleNotFoundError: No module named 'agent_core.mcp.manifest_cache'
```

- [ ] **Step 3: Implement the minimal manifest-cache module**

```python
import json
from pathlib import Path
from typing import Any, Dict, Optional


def _manifest_dir(instance_dir: Path) -> Path:
    return instance_dir / ".mcp"


def _manifest_path(instance_dir: Path, server_id: str) -> Path:
    return _manifest_dir(instance_dir) / f"{server_id}.manifest.json"


def load_manifest_cache(instance_dir: Path, server_id: str) -> Optional[Dict[str, Any]]:
    path = _manifest_path(instance_dir, server_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest_cache(instance_dir: Path, server_id: str, payload: Dict[str, Any]) -> Path:
    root = _manifest_dir(instance_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = _manifest_path(instance_dir, server_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
```

- [ ] **Step 4: Add a missing-file behavior test**

```python
def test_manifest_cache_missing_returns_none(tmp_path: Path):
    assert load_manifest_cache(tmp_path, "iwms") is None
```

- [ ] **Step 5: Run the manifest-cache test file**

Run:

```bash
python -m pytest tests/test_mcp_manifest_cache.py -q
```

Expected:

```text
2 passed
```

- [ ] **Step 6: Commit the manifest-cache module**

```bash
git add src/agent_core/mcp/manifest_cache.py tests/test_mcp_manifest_cache.py
git commit -m "feat: add MCP manifest cache persistence"
```

---

### Task 4: Extend MCPManager with Manifest-Aware Status and Gated Execution

**Files:**
- Modify: `src/agent_core/mcp/manager.py`
- Modify: `tests/test_mcp_awareness.py`

- [ ] **Step 1: Write the failing awareness test for cached manifest + error state**

```python
from pathlib import Path

from agent_core.mcp.manager import MCPManager
from agent_core.mcp.manifest_cache import save_manifest_cache


def test_build_awareness_summary_uses_manifest_when_server_is_error(tmp_path: Path, monkeypatch):
    inst = tmp_path / "inst"
    inst.mkdir()
    save_manifest_cache(
        inst,
        "capture",
        {
            "server_id": "capture",
            "title": "capture",
            "description": "Capture service MCP",
            "actions": [{"name": "capture_api_call", "description": "generic fallback"}],
        },
    )
    monkeypatch.setenv("INSTANCE_DIR", str(inst))
    manager = MCPManager()
    manager._servers = {}
    manager._cache["capture"] = manager._cache.get("capture") or type("C", (), {})()
```

- [ ] **Step 2: Replace the test with a concrete manager cache fixture and assert `status=error` still renders manifest data**

```python
def test_build_awareness_summary_uses_manifest_when_server_is_error(tmp_path: Path, monkeypatch):
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / "config.yaml").write_text("mcp:\n  enabled_only:\n    - capture\n", encoding="utf-8")
    global_cfg = tmp_path / "global.yaml"
    global_cfg.write_text(
        "mcp_servers:\n"
        "  capture:\n"
        "    transport: sse\n"
        "    url: http://127.0.0.1:8000/mcp/sse\n"
        "    description: Capture service MCP\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("agent_core.mcp.config._default_global_config_path", lambda: global_cfg)
    monkeypatch.setenv("INSTANCE_DIR", str(inst))
    save_manifest_cache(
        inst,
        "capture",
        {
            "server_id": "capture",
            "title": "capture",
            "description": "Capture service MCP",
            "actions": [{"name": "capture_api_call", "description": "generic fallback"}],
        },
    )

    manager = MCPManager()
    manager.refresh_all(inst)
    manager._cache["capture"].status = "error"
    manager._cache["capture"].last_error = "connection refused"

    block = manager.build_awareness_summary(inst)

    assert "capture" in block
    assert "status=error" in block
    assert "Capture service MCP" in block
```

- [ ] **Step 3: Add manifest-loading and status-normalization helpers in `manager.py`**

```python
from .manifest_cache import load_manifest_cache, save_manifest_cache


def _manifest_payload_from_tools(cfg: MCPServerConfig, tools: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "server_id": cfg.server_id,
        "title": cfg.title or cfg.server_id,
        "description": cfg.description or "",
        "transport": cfg.transport,
        "endpoint": cfg.url,
        "actions": [
            {
                "name": name,
                "description": str(tool.get("description") or ""),
                "inputSchema": tool.get("inputSchema") or {},
            }
            for name, tool in sorted(tools.items())
        ],
    }
```

- [ ] **Step 4: Persist manifest on successful refresh and mark richer statuses**

```python
tools = _extract_tools(result)
manifest = _manifest_payload_from_tools(cfg, tools)
save_manifest_cache(inst, server_id, manifest)
cache.status = "ready"
cache.last_error = ""
cache.tools = tools
```

```python
if not ok:
    manifest = load_manifest_cache(inst, server_id)
    cache.status = "stale" if manifest else "error"
    cache.last_error = err or "list_tools failed"
    self._unregister_server_tools(server_id)
    return
```

- [ ] **Step 5: Build awareness from config + manifest + runtime status instead of runtime-only tools**

```python
manifest = load_manifest_cache(inst, cfg.server_id) or {}
actions = manifest.get("actions") or []
lines.append(f"- `{label}` ({cfg.transport}): {desc} [status={status}]")
if actions:
    preview = ", ".join(a["name"] for a in actions[:5])
    lines.append(f"actions: {preview}")
if status in {"error", "stale"} and cache.last_error:
    lines.append(f"error: {cache.last_error}")
```

- [ ] **Step 6: Ensure executable schemas are registered only for `ready`**

```python
if cache.status != "ready":
    self._unregister_server_tools(server_id)
    return
```

- [ ] **Step 7: Run the awareness and manager test suite**

Run:

```bash
python -m pytest tests/test_mcp_awareness.py -q
```

Expected:

```text
all MCP awareness tests pass
```

- [ ] **Step 8: Commit the manager/awareness change**

```bash
git add src/agent_core/mcp/manager.py tests/test_mcp_awareness.py src/agent_core/mcp/manifest_cache.py tests/test_mcp_manifest_cache.py
git commit -m "feat: add manifest-aware MCP status handling"
```

---

### Task 5: Add CLI Manifest Diagnostics and Document Unified Statuses

**Files:**
- Modify: `src/agent_core/cli.py`
- Modify: `docs/usage.md`

- [ ] **Step 1: Write the failing CLI import/dispatch test for `mcp manifest`**

```python
from argparse import Namespace

from agent_core.cli import build_parser


def test_cli_registers_mcp_manifest_subcommand():
    parser = build_parser()
    args = parser.parse_args(["mcp", "manifest", "-I", "dummy"])
    assert args.func.__name__ == "_cmd_mcp_manifest"
```

- [ ] **Step 2: Run the focused CLI test**

Run:

```bash
python -m pytest tests/test_mcp_awareness.py::test_cli_registers_mcp_manifest_subcommand -q
```

Expected:

```text
FAIL until the subcommand is added
```

- [ ] **Step 3: Add `_cmd_mcp_manifest` in `cli.py`**

```python
def _cmd_mcp_manifest(args) -> int:
    inst = _instance_dir_from_args(args)
    servers = resolve_servers(inst)
    rows = []
    from agent_core.mcp.manifest_cache import load_manifest_cache

    for s in servers:
        manifest = load_manifest_cache(inst, s.server_id) or {}
        actions = manifest.get("actions") or []
        rows.append(
            {
                "server_id": s.server_id,
                "transport": s.transport,
                "has_manifest": bool(manifest),
                "actions_count": len(actions),
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0
```

- [ ] **Step 4: Register the subcommand in the parser**

```python
p_manifest = mcp_sub.add_parser("manifest", help="show local MCP manifest cache")
p_manifest.add_argument("--instance-dir", "-I", default=None, help="instance directory (path or name)")
p_manifest.set_defaults(func=_cmd_mcp_manifest)
```

- [ ] **Step 5: Update `docs/usage.md` with the new status model and Capture SSE note**

```md
## MCP Runtime States

- `known`: configured and recognized, but live refresh has not completed
- `loading`: refresh in progress
- `ready`: manifest is current and executable tool schema is registered
- `stale`: manifest exists, but latest live refresh is not current
- `error`: service exists but is not currently executable

## Capture MCP

`capture` now runs as an SSE MCP service and should be configured like `iwms`, not as a local stdio subprocess.
```

- [ ] **Step 6: Run the focused CLI/documentation checks**

Run:

```bash
python -m pytest tests/test_mcp_awareness.py::test_cli_registers_mcp_manifest_subcommand -q
```

Expected:

```text
1 passed
```

- [ ] **Step 7: Commit the CLI/usage update**

```bash
git add src/agent_core/cli.py docs/usage.md
git commit -m "feat: add MCP manifest diagnostics"
```

---

### Task 6: End-to-End Verification for Capture and IWMS as Unified SSE MCP Services

**Files:**
- Modify: `tests/test_mcp_official_clients.py`
- Modify: `build.spec` (only if verification reveals packaging gaps)

- [ ] **Step 1: Add the failing contract test that assumes Capture is SSE-backed**

```python
from agent_core.mcp.config import MCPServerConfig
from agent_core.mcp.http_sse import MCPHttpSSEClient


def test_capture_sse_client_exposes_existing_sync_contract():
    cfg = MCPServerConfig(
        server_id="capture",
        transport="sse",
        url="http://127.0.0.1:8000/mcp/sse",
        headers={},
        timeout_seconds=5,
    )
    client = MCPHttpSSEClient(cfg)

    assert hasattr(client, "probe")
    assert hasattr(client, "list_tools")
    assert hasattr(client, "call_tool")
```

- [ ] **Step 2: Run the focused official-client test**

Run:

```bash
python -m pytest tests/test_mcp_official_clients.py::test_capture_sse_client_exposes_existing_sync_contract -q
```

Expected:

```text
PASS after Capture SSE service is available
```

- [ ] **Step 3: Verify live Capture and IWMS tool discovery**

Run:

```bash
python -c "from pathlib import Path; from agent_core.mcp.config import resolve_servers; from agent_core.mcp.http_sse import MCPHttpSSEClient; inst=Path(r'C:\Users\Administrator\.nanoghost\instances\cc'); s=resolve_servers(inst)[0]; c=MCPHttpSSEClient(s); print(c.probe()); print(c.list_tools()[0], len(c.list_tools()[1].get('tools', [])))"
python -c "from pathlib import Path; from agent_core.mcp.config import resolve_servers; from agent_core.mcp.http_sse import MCPHttpSSEClient; inst=Path(r'C:\Users\Administrator\.nanoghost\instances\iwms'); s=resolve_servers(inst)[0]; c=MCPHttpSSEClient(s); print(c.probe()); print(c.list_tools()[0], len(c.list_tools()[1].get('tools', [])))"
```

Expected:

```text
MCPProbeResult(ok=True, status='connected', ...)
True <non-zero tool count>
```

- [ ] **Step 4: Verify folded executable schemas appear only when services are ready**

Run:

```bash
python -c "import os,time; os.environ['INSTANCE_DIR']=r'C:\Users\Administrator\.nanoghost\instances\cc'; from tests.test_basic import MockDatabase, MockLLM; from agent_core import Agent; a=Agent(db=MockDatabase(), llm=MockLLM(), auto_discover_skills=False); time.sleep(3); print([x['function']['name'] for x in a.tool_registry.get_available_schemas() if 'mcp' in x['function']['name']])"
python -c "import os,time; os.environ['INSTANCE_DIR']=r'C:\Users\Administrator\.nanoghost\instances\iwms'; from tests.test_basic import MockDatabase, MockLLM; from agent_core import Agent; a=Agent(db=MockDatabase(), llm=MockLLM(), auto_discover_skills=False); time.sleep(3); print([x['function']['name'] for x in a.tool_registry.get_available_schemas() if 'mcp' in x['function']['name']])"
```

Expected:

```text
['mcp_capture']
['mcp_iwms']
```

- [ ] **Step 5: Run the focused regression suite**

Run:

```bash
python -m pytest tests/test_mcp_official_clients.py tests/test_mcp_awareness.py tests/test_mcp_manifest_cache.py tests/test_prompt_layering.py tests/test_memory_files.py -q
```

Expected:

```text
all targeted tests pass
```

- [ ] **Step 6: Update packaging only if verification shows missing imports or data**

```python
hiddenimports=[
    "mcp",
    "mcp.client",
    "mcp.client.session",
    "mcp.client.sse",
    "mcp.server",
    "mcp.server.sse",
    "mcp.types",
]
```

- [ ] **Step 7: Commit the end-to-end unified MCP verification**

```bash
git add tests/test_mcp_official_clients.py tests/test_mcp_awareness.py tests/test_mcp_manifest_cache.py build.spec docs/usage.md
git commit -m "feat: unify capture and iwms as service-style MCP"
```

---

## Self-Review

### Spec Coverage

- Capture moves from stdio to SSE: covered by Task 1 and Task 2
- NanoGhost gets manifest cache: covered by Task 3
- Runtime status expands to `known/loading/ready/stale/error`: covered by Task 4 and Task 5
- Awareness and execution remain separate: covered by Task 4 and Task 6
- Folded `mcp_<server_id>` tools remain unchanged conceptually: covered by Task 4 and Task 6
- Diagnostics include manifest cache visibility: covered by Task 5

### Placeholder Scan

- No `TODO`, `TBD`, or "implement later" placeholders remain
- Every task includes exact file paths
- Every code-changing step includes concrete code blocks
- Every verification step includes exact commands and expected outputs

### Type Consistency

- `manifest` is always a persisted JSON payload with `server_id/title/description/actions`
- `status` is consistently described as `known/loading/ready/stale/error`
- executable tool registration is always gated on `ready`
- Capture and IWMS are both verified through `MCPHttpSSEClient`

---

**Plan complete and saved to `docs/superpowers/plans/2026-07-06-capture-sse-manifest-unification.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
