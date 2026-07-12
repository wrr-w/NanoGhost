# NanoGhost Official MCP Client Replacement Design

> Version: v1 - 2026-06-11
> Status: Draft Design
> Scope: Replace NanoGhost custom MCP transport clients with official Python `mcp` client implementations for both HTTP/SSE and stdio.

---

## 1. Background

NanoGhost currently keeps its own MCP transport implementations:

- `src/agent_core/mcp/http_sse.py`
- `src/agent_core/mcp/stdio_client.py`

These clients expose a simple synchronous interface to the rest of the system:

- `probe()`
- `list_tools()`
- `call_tool()`

This design worked for basic servers, but it now shows protocol incompatibility with official MCP SSE servers such as IWMS, which uses `mcp.server.sse.SseServerTransport`.

Recent investigation confirmed:

- The IWMS server is up and exposes `/mcp/sse`
- The server emits the expected `endpoint` SSE event
- NanoGhost custom transport logic still fails to complete a working MCP tool round-trip
- The issue is no longer just configuration or prompt visibility; it is a transport-level compatibility problem

The conclusion is:

> NanoGhost should stop maintaining a custom MCP protocol implementation where an official client already exists.

---

## 2. Goal

Replace NanoGhost custom MCP transport clients with thin adapters over the official Python `mcp` client library, while preserving NanoGhost's existing upper-layer architecture.

The replacement should cover:

- HTTP/SSE MCP servers
- stdio MCP servers

The replacement should **not** require a redesign of:

- `MCPManager`
- `ToolRegistry`
- folded `mcp_<server_id>` tool exposure
- MCP awareness summary / status summary
- instance whitelist and action allowlist logic

---

## 3. Primary Design Decision

### 3.1 Keep Upper Layers, Replace Transport Layer

Retain:

- `src/agent_core/mcp/config.py`
- `src/agent_core/mcp/manager.py`
- current folded MCP tool model
- current awareness summary model
- current runtime cache model

Replace:

- custom HTTP/SSE protocol logic
- custom stdio protocol logic

with:

- official `mcp` Python client library

### 3.2 No Fallback Path

This change intentionally does **not** keep the custom clients as a fallback.

Reason:

- fallback increases complexity
- fallback makes diagnosis harder
- the goal of this change is to stop carrying protocol responsibility in NanoGhost

---

## 4. Architecture

### 4.1 Existing Contract to Preserve

The rest of NanoGhost expects transport clients to support:

- `probe() -> MCPProbeResult`
- `list_tools() -> (ok, result, err, duration_ms)`
- `call_tool(name, arguments) -> (ok, result, err, duration_ms)`

This contract should remain stable so that `MCPManager` can stay mostly unchanged.

### 4.2 New Adapter Layer

Introduce a thin adapter layer that wraps official `mcp` client calls and presents the same synchronous contract.

Suggested structure:

- `src/agent_core/mcp/client_types.py`
  - shared lightweight result types / protocol helpers if needed
- `src/agent_core/mcp/http_sse.py`
  - no longer hand-rolls SSE; becomes an adapter around official HTTP/SSE client behavior
- `src/agent_core/mcp/stdio_client.py`
  - no longer hand-rolls stdio JSON-RPC; becomes an adapter around official stdio client behavior

The filenames can remain the same to minimize imports and reduce the size of the change.

### 4.3 Synchronous Outer Interface

Even if the official `mcp` client is async-first, NanoGhost should keep a synchronous-facing transport interface.

Reason:

- `MCPManager` is currently synchronous
- CLI MCP commands are currently synchronous
- moving the entire MCP management flow to async is unnecessary for this change

Therefore:

- async work happens inside the adapter layer
- the adapter blocks locally and returns a sync result to callers

This is the smallest architectural change.

---

## 5. File-level Plan

### 5.1 `src/agent_core/mcp/http_sse.py`

Change from:

- custom `requests`-based SSE endpoint/session handling

Change to:

- official `mcp` client-backed adapter

Responsibilities after change:

- create official HTTP/SSE client session
- initialize connection
- fetch tools
- call tool
- translate official client responses into NanoGhost's existing tuple contract

Must no longer do:

- manual SSE event parsing
- manual endpoint extraction
- manual session lifecycle handling

### 5.2 `src/agent_core/mcp/stdio_client.py`

Change from:

- custom `subprocess.Popen` + newline JSON transport implementation

Change to:

- official stdio MCP client-backed adapter

Responsibilities after change:

- start stdio transport via official client
- initialize connection
- fetch tools
- call tool
- translate official client responses into NanoGhost's existing tuple contract

Must no longer do:

- manual reader thread
- manual pending request maps
- manual request id synchronization

### 5.3 `src/agent_core/mcp/manager.py`

Keep as-is conceptually.

Only expected changes:

- import paths if needed
- minor response-shape normalization if official client returns slightly different tool/result structures

Must keep:

- whitelist handling
- action allowlist filtering
- folded `mcp_<server_id>` tool registration
- status cache
- cooldown / fail threshold behavior

### 5.4 `src/agent_core/cli.py`

Keep commands and UX intact.

Only expected changes:

- `mcp tools`
- `mcp reload`
- `mcp probe`

should rely on the new adapters and behave consistently for both stdio and SSE transports.

Current special-case mistakes in CLI should be removed as part of this change if touched, especially paths that assume SSE only.

### 5.5 Dependency Files

Update the project dependency declaration to include the official `mcp` client dependency.

Targets to update depending on current project packaging strategy:

- `requirements.txt`
- `pyproject.toml`
- PyInstaller hidden imports / packaging rules if needed

This is required because current packaging is explicit in several places.

---

## 6. Behavior Requirements

### 6.1 HTTP/SSE

For an official server like IWMS:

- `probe()` should succeed
- `list_tools()` should return a proper MCP tools payload
- `MCPManager.refresh_server()` should register folded tools
- `mcp_iwms` should appear in `get_available_schemas()`

### 6.2 stdio

For an official stdio-like server already used by NanoGhost, such as `capture`:

- `probe()` should still succeed
- `list_tools()` should still return tools
- existing instance configs should not require schema changes

### 6.3 Awareness Layer

MCP awareness summary remains a separate concern and should continue to work regardless of transport replacement.

The replacement must not break:

- enabled MCP recognition
- loading/ready/error state summary
- folded tool visibility rules

---

## 7. Risks

### 7.1 Official Client Async Model

The official `mcp` client may be async-first.

Risk:

- forcing it into sync wrappers can create lifecycle bugs if not carefully scoped

Mitigation:

- keep the async boundary inside each adapter call
- ensure connections/sessions are managed consistently per request or per adapter lifecycle

### 7.2 Packaging Impact

NanoGhost already uses PyInstaller and custom hidden import handling.

Risk:

- official `mcp` dependency may introduce new hidden imports or runtime requirements

Mitigation:

- update packaging config together with the transport replacement
- add one frozen-mode verification pass after implementation

### 7.3 Response Shape Differences

Official `mcp` client may return richer or differently structured tool/result objects.

Risk:

- `_extract_tools()` and `call_tool()` payload assembly may assume current custom shape

Mitigation:

- normalize official results at adapter boundary
- keep upper layers unaware of official client specifics

---

## 8. Non-goals

- Do not redesign prompt injection
- Do not redesign MCP awareness summary
- Do not change the folded tool model into one-tool-per-MCP-action
- Do not redesign `MCPManager` into async architecture
- Do not add fallback to old custom clients

---

## 9. Verification Plan

The replacement is considered successful when all of the following pass:

### 9.1 Unit / focused tests

- HTTP/SSE adapter test against official client-backed path
- stdio adapter test against official client-backed path
- `MCPManager` refresh path still registers tools correctly

### 9.2 CLI verification

- `nanoghost mcp probe -I <instance>`
- `nanoghost mcp tools <server_id> -I <instance>`
- `nanoghost mcp reload -I <instance>`

must work correctly for both:

- `iwms` (SSE)
- `capture` (stdio)

### 9.3 Runtime verification

- `iwms` instance should expose `mcp_iwms`
- `capture` instance should still expose `mcp_capture`
- awareness summary should remain present in the LLM context

---

## 10. Summary

This design intentionally does the minimum architectural work necessary:

- keep NanoGhost MCP management model
- replace only the fragile custom transport implementations
- standardize on the official Python `mcp` client for both SSE and stdio

That gives NanoGhost the smallest change with the highest protocol compatibility payoff.
