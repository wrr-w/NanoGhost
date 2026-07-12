# Capture SSE Manifest Unification Design

> Version: v1 - 2026-07-06
> Status: Draft Design
> Scope: Convert `capture` from local stdio MCP to service-style SSE MCP and unify NanoGhost MCP cognition/runtime handling across `capture` and `iwms`.

---

## 1. Background

NanoGhost currently integrates MCP servers in two different shapes:

- `capture` uses `stdio`
- `iwms` uses `sse`

Although both are exposed to the agent as MCP, they behave very differently at runtime:

- `capture` can often expose tools even when its downstream HTTP backend is unavailable, because its MCP tool list is generated locally from static API metadata
- `iwms` only becomes usable after NanoGhost successfully connects to the remote SSE MCP service and completes tool discovery

This creates two user-visible problems:

1. The agent's MCP awareness is inconsistent across servers
2. Runtime status can become misleading:
   - one MCP appears to "exist" even when its real backend is down
   - another MCP appears "error" even though the service has already recovered

The desired direction is:

> `capture` and `iwms` should both look like the same category of MCP service to NanoGhost: service-style MCP with stable cognition, explicit runtime status, and on-demand execution.

---

## 2. Goal

Establish a unified MCP model in NanoGhost with three distinct layers:

- `manifest` layer: what the MCP is, what actions it exposes, and what the action shape looks like
- `runtime status` layer: whether the MCP is currently `known`, `loading`, `ready`, `stale`, or `error`
- `execution` layer: whether the MCP is currently allowed to appear in executable tool schemas

Under this design:

- `capture` is no longer treated as a special local stdio MCP
- `capture` is exposed as an SSE MCP service, like `iwms`
- NanoGhost retains a local cognition/cache layer so the model can know a server exists before it is currently ready
- only `ready` MCP servers are registered as executable folded tools such as `mcp_capture` or `mcp_iwms`

---

## 3. Primary Design Decision

### 3.1 Treat MCP Transport as an Execution Detail

The important abstraction is not `stdio` versus `sse`.

The important abstraction is:

- local stable cognition
- live runtime status
- gated execution

Therefore, transport is only part of the execution layer.

### 3.2 Standardize on Service-style MCP for Capture

`capture` should be moved from:

- local subprocess-style `stdio` MCP

to:

- service-style official `SSE MCP`

This makes `capture` operationally consistent with `iwms`:

- both are independent MCP services
- both are discovered over SSE
- both have live runtime reachability
- both can refresh their tool definitions over time

### 3.3 Keep Folded Tool Exposure

NanoGhost should keep the current folded tool model:

- one exposed tool per MCP server
- action routing inside that tool

Examples:

- `mcp_capture`
- `mcp_iwms`

This avoids prompt bloat and keeps the current tool-calling interface stable.

---

## 4. Architecture

### 4.1 Three-layer MCP Model

Each MCP server in NanoGhost should be represented by three related but distinct objects:

#### A. Manifest

The local manifest records what NanoGhost knows about the MCP server.

Suggested fields:

- `server_id`
- `title`
- `description`
- `transport`
- `endpoint`
- `actions`
- `action parameter summary`
- `last_manifest_refresh_at`
- `manifest_source`

This layer exists for cognition and survives temporary runtime failures.

#### B. Runtime Status

The runtime status records current health and freshness.

Suggested states:

- `known`
- `loading`
- `ready`
- `stale`
- `error`

Suggested fields:

- `status`
- `last_probe_at`
- `last_success_at`
- `last_error`
- `cooldown_until`
- `tools_count`

This layer exists for truthful user/model feedback.

#### C. Executable Tool Registration

This is the actual tool schema registration surface.

Only servers in `ready` state should register executable folded tools into `ToolRegistry`.

That means:

- the model may know an MCP exists before it is executable
- the model may know an MCP is temporarily unavailable without losing awareness of its role

### 4.2 Awareness Versus Execution

NanoGhost should continue to separate:

- awareness injected into system context
- executable tools passed into LLM tool schemas

Awareness should come from:

- local manifest
- current runtime status

Execution should come from:

- current `ready` folded tool schemas only

This ensures:

- the model is not blind
- the model is not allowed to call unavailable MCP servers

---

## 5. Component Boundaries

### 5.1 Capture Repository

The Capture project must expose a real SSE MCP service.

Required shape:

- official `mcp.server.sse.SseServerTransport`
- `/mcp/sse`
- `/mcp/messages`

The Capture service may continue to generate tool definitions from its existing `api_spec.json`, but the public MCP surface should now be SSE instead of stdio.

This change turns Capture into an independent MCP service rather than a subprocess launched by NanoGhost.

### 5.2 NanoGhost MCP Config

NanoGhost configuration should stop treating `capture` as:

- `command`
- `args`
- `stdio`

and instead treat it like `iwms`:

- `url`
- `transport: sse`

This makes the registry model uniform.

### 5.3 NanoGhost MCP Manager

`MCPManager` remains the coordinator for:

- server loading
- refresh
- runtime status
- folded tool registration

It must be extended so that it manages both:

- live runtime cache
- local manifest cache

The manager should no longer treat tool discovery as purely ephemeral.

### 5.4 Awareness Builder

The MCP awareness summary should be built from:

- manifest cache
- current runtime status

not only from "whatever happened to be connected during this refresh".

That allows the model to see:

- which MCP servers exist
- what they are for
- whether they are ready, stale, loading, or error

### 5.5 Tool Registry

`ToolRegistry` should continue to receive only executable folded tools.

No change in concept:

- `mcp_capture`
- `mcp_iwms`

No direct one-tool-per-action expansion is introduced in this design.

---

## 6. Data Flow

### 6.1 Startup

At startup, NanoGhost should:

1. load MCP registry config
2. build or load local manifest cache entries for all enabled MCP servers
3. initialize runtime status as `known` or `loading`
4. immediately expose awareness context to the model
5. asynchronously refresh live tool definitions

This means first-turn cognition is not blocked on first-turn connectivity.

### 6.2 Refresh

During refresh, NanoGhost should:

1. connect to the MCP service over SSE
2. run `list_tools()`
3. normalize the returned tool definitions
4. update local manifest cache
5. update runtime status
6. register or update folded executable tools if the server is `ready`

If refresh fails:

- keep manifest cache
- update runtime status to `error` or `stale`
- remove executable tool registration if the runtime state should no longer be callable

### 6.3 Per-turn Agent Behavior

Each turn should use:

- awareness block from `manifest + status`
- executable tool schemas from current `ready` registrations only

The agent should not re-query remote MCP definitions on every turn.

Remote refresh should be triggered by:

- startup
- explicit reload
- TTL expiry
- targeted recovery from `loading/error/stale`

---

## 7. Status Model

The status model should become more expressive than the current `loading/connected/error` simplification.

Recommended meanings:

- `known`: server is configured and manifest exists, but no live attempt has completed yet
- `loading`: refresh is in progress
- `ready`: latest live refresh succeeded and executable tool schemas are registered
- `stale`: manifest exists from a prior success, but the latest refresh has not completed successfully or freshness TTL has expired
- `error`: the latest live refresh failed and no trusted current runtime execution state is available

Recommended awareness behavior:

- `known` and `loading`: visible to the model as existing but not yet callable
- `ready`: visible and callable
- `stale`: visible as existing with possibly outdated schema; callable only if policy explicitly allows cached execution, which this design does not recommend by default
- `error`: visible as existing but not callable

---

## 8. Storage Strategy

### 8.1 Manifest Cache

NanoGhost should persist a lightweight local manifest cache for each MCP server.

Persistence options:

- instance-local file cache under the instance directory
- or a dedicated MCP cache directory under NanoGhost state

Recommended contents:

- server identity
- human-readable description
- action names
- action summaries
- parameter summaries
- last successful refresh timestamp

The cache should be safe to read even when the live service is currently down.

### 8.2 Runtime Status Cache

Runtime status may remain in memory for fast operation, but should be serializable for diagnostics if useful.

This is primarily operational state rather than durable knowledge.

---

## 9. Migration Plan

### 9.1 Capture Service Migration

Replace the current stdio MCP entry path in Capture with SSE MCP transport.

The actual tool-generation logic can remain largely the same:

- still generate tools from `api_spec.json`
- still keep the generic fallback action if needed

But the transport and exposure model changes to SSE service hosting.

### 9.2 NanoGhost Config Migration

Update global MCP config so `capture` is declared similarly to `iwms`:

- remove stdio command/args
- add SSE URL

### 9.3 NanoGhost Runtime Migration

Update NanoGhost so all enabled MCP servers are handled through the unified manifest/status/execution model.

This should apply to both:

- `capture`
- `iwms`

and to future MCP servers as well.

---

## 10. Non-goals

- Do not redesign the folded `mcp_<server_id>` tool exposure model
- Do not expand MCP actions into one top-level tool per action
- Do not redesign the whole agent loop into async-first architecture
- Do not require first-turn full MCP readiness before first-turn cognition
- Do not treat local manifest cache as proof that a service is currently executable

---

## 11. Risks

### 11.1 Capture Becomes Operationally Stricter

After moving to SSE, `capture` will behave more like a real service dependency.

That is desired, but it means:

- service startup order matters more
- network/endpoint failures become visible earlier

Mitigation:

- stable manifest cache
- clearer status model
- explicit reload and diagnostics

### 11.2 Stale Schema Confusion

If local manifest cache is old, the model may know actions that are not currently available on the live server.

Mitigation:

- expose `stale` explicitly
- only allow execution for `ready`
- include freshness timestamps in diagnostics

### 11.3 More Files and State to Manage

Manifest persistence introduces another cache layer.

Mitigation:

- keep the cache lightweight
- derive it directly from normalized tool definitions
- avoid separate authoring or manual editing workflows

---

## 12. Verification Plan

The design is successful when all of the following are true:

### 12.1 Capture Service

- Capture exposes official SSE MCP endpoints
- NanoGhost can connect with the same SSE client path used for `iwms`
- `list_tools()` returns Capture tools over SSE

### 12.2 Unified Cognition

- Agent first-turn awareness includes both `capture` and `iwms`
- awareness remains present even when a live refresh temporarily fails
- awareness displays state truthfully as `known/loading/ready/stale/error`

### 12.3 Unified Execution Rules

- only `ready` servers appear in executable tool schemas
- `mcp_capture` and `mcp_iwms` are both folded tools
- no server is callable merely because a manifest exists

### 12.4 Diagnostics

- CLI can show live probe state
- CLI can show current executable tools
- CLI can show local manifest cache state

---

## 13. Summary

This design does not merely switch `capture` from `stdio` to `sse`.

It establishes a single MCP mental model for NanoGhost:

- local manifest for cognition
- live runtime status for truth
- gated folded tools for execution

Under that model:

- `capture` and `iwms` are the same category of MCP service
- the model always knows enabled MCP servers exist
- execution remains safe and status-aware
- future MCP servers can plug into one uniform path
