# NanoGhost Memory / MCP Layering Design

> Version: v1 - 2026-06-09
> Status: Draft Design
> Scope: Reorganize prompt assembly, memory layering, MCP capability awareness, and runtime readiness visibility.

---

## 1. Background

The current Agent runtime mixes several concerns into one prompt path:

- Stable identity and behavior rules
- Long-term memory and short-lived working memory
- Skill awareness and MCP awareness
- Runtime execution capability and runtime readiness

This causes two recurring problems:

1. `memory.md` is treated as a catch-all store, so long-term facts and same-day working context are injected together.
2. MCP is only visible when tools are already registered into the runtime schema, so the model can become "blind" to enabled MCP servers before they are ready.

The goal of this design is **not** to force all MCP servers to be ready before the first reply. The goal is:

- The model should know what enabled MCP servers exist and what they are for.
- The model should know whether an MCP is ready or not ready.
- The model should only call MCP tools when they are truly available in runtime schemas.
- Long-term memory and same-day working memory should be separated.

---

## 2. Design Goals

### 2.1 Primary Goals

- Split memory into:
  - long-term descriptive memory
  - same-day short-term working memory
- Separate MCP awareness from MCP execution readiness.
- Make the prompt assembly model explicit and layered.
- Preserve existing execution semantics: only registered tool schemas are callable.

### 2.2 Non-goals

- Do not redesign the entire Agent loop.
- Do not require all enabled MCP servers to block first response.
- Do not introduce a DB-backed memory store in this phase.
- Do not change the current MCP "one server => one folded tool" execution model.
- Do not implement cross-day summarization, archival, or compaction in this phase.

---

## 3. Layered Model

The runtime model is split into six layers.

### 3.1 Identity Layer

Defines who the Agent is, what rules it follows, and which channel identity it is using.

Sources:

- `prompts/agent_profile.md`
- `prompts/agent_rules_conduct.md`
- channel/session context such as source platform, bot name, lark-cli profile, sender identity

This layer answers:

- Who am I?
- What rules constrain my behavior?
- In which channel identity am I currently speaking?

### 3.2 Memory Layer

Stores what the Agent should remember, split into two types.

- Long-term memory
  - stable facts
  - user preferences
  - durable operational rules
  - long-lived project facts
- Daily memory
  - today's task state
  - today's decisions
  - today's pending follow-ups
  - temporary continuation context

This layer answers:

- What stable facts should I always remember?
- What is relevant for today only?

### 3.3 Capability Awareness Layer

Exposes what the Agent knows it can potentially use.

This layer is **awareness**, not execution.

It contains:

- skill awareness index
- MCP capability awareness index

This layer answers:

- Which skills exist?
- Which enabled MCP servers exist?
- What are they generally used for?

### 3.4 Tool Execution Layer

Exposes what the Agent can actually call in the current LLM round.

It contains:

- builtin tools
- skill tools
- MCP runtime tools such as `mcp_capture`

This layer answers:

- Which tools are truly callable right now?

### 3.5 Runtime State Layer

Represents current system control-plane state.

It contains:

- MCP readiness
- MCP last error summary
- MCP current status (`unknown`, `loading`, `ready`, `error`, `cooldown`)
- related runtime snapshots

This layer answers:

- What is the system's current runtime status?

### 3.6 Core Loop Layer

Connects the previous layers into one execution flow.

It answers:

- In what order should identity, memory, awareness, and tools be assembled before calling the LLM?

---

## 4. Memory Model

### 4.1 Long-term Memory

File:

- `INSTANCE_DIR/memory.md`

Purpose:

- Descriptive long-term memory only

Allowed content:

- stable user preferences
- instance role definition
- long-term project facts
- durable rules and habits
- long-lived lessons learned

Disallowed content:

- today's task progress
- temporary status
- in-progress work notes
- same-day conversational continuity

### 4.2 Daily Memory

Directory and file format:

- `INSTANCE_DIR/memory.daily/YYYY-MM-DD.md`

Purpose:

- Same-day working memory only

Allowed content:

- today's working state
- today's decisions
- today's "continue from here" notes
- short-lived execution context

Default behavior:

- Only today's file is auto-injected
- Previous days are not auto-injected
- Older files remain available for later manual inspection or future retrieval features

### 4.3 Memory Write Routing

The write contract should become explicit:

- long-term facts -> `memory.md`
- same-day working context -> `memory.daily/<today>.md`

This can be expressed either by:

- explicit target selection in `memory_write`
- or a routing layer that maps write intent to the correct store

This spec does not require the final API shape yet, but the routing behavior is required.

---

## 5. MCP Awareness vs MCP Readiness

### 5.1 Problem Statement

Current behavior conflates:

- "enabled in config"
- "known by the model"
- "ready in runtime"
- "callable in current round"

These are not the same thing.

### 5.2 Required Separation

The design introduces two different MCP surfaces.

#### A. MCP Capability Awareness Surface

Visible to the model even before runtime tools are ready.

For every MCP server in instance `enabled_only`, expose lightweight metadata such as:

- `server_id`
- transport
- short description
- typical use cases
- current status summary

This gives the model non-blind awareness:

- "capture exists"
- "capture is for X"
- "capture is currently loading/ready/error"

#### B. MCP Tool Execution Surface

Visible only when runtime registration is complete.

This remains unchanged:

- only tools returned by `get_available_schemas()` are callable

Therefore:

- known != callable
- enabled != ready
- ready summary != guaranteed execution unless schema is registered

### 5.3 MCP Status States

Normalized model-facing state values:

- `unknown`
- `loading`
- `ready`
- `error`
- `cooldown`

Internal MCP cache may use richer implementation details, but the model-facing summary should stay lightweight.

---

## 6. Prompt and Message Assembly Model

### 6.1 Base Prompt

The base prompt should contain only stable layers:

- `agent_profile.md`
- `agent_rules_conduct.md`
- long-term memory from `memory.md`

It should **not** contain daily working memory.

### 6.2 Per-turn System Additions

Per turn, the following should be appended in order:

1. session/channel context
2. daily memory block for today
3. skill awareness index
4. MCP capability awareness index
5. MCP current readiness summary
6. retrieved similar flow summaries
7. session image index

### 6.3 Tool Schema Exposure

Tool schemas remain separate from prompt text.

At LLM call time:

- runtime builds current `tools schema`
- only those schemas represent truly callable tools

Prompt text may say an MCP exists and is loading, but execution is allowed only after schema registration.

---

## 7. Core Loop

The target per-turn loop should be understood as:

```text
User input
-> build identity layer
-> inject long-term memory
-> inject session context
-> inject today's daily memory
-> inject skill awareness
-> inject MCP awareness + MCP status summary
-> inject history/image runtime augmentations
-> fetch current tool schemas
-> LLM decision
-> tool execution / final reply
-> write memory to correct target store
```

Key rule:

- Awareness can exist before readiness
- Readiness can exist before actual use
- Only registered tool schemas are executable

---

## 8. File-level Change Plan

### 8.1 `run.py`

Change:

- `assemble_sys_prompt()` only assembles stable prompt content.
- Keep long-term memory injection here.
- Stop treating base prompt assembly as the place for short-term working memory.

Do not change:

- overall entry structure for CLI / Feishu

### 8.2 `src/agent_core/channel/instance.py`

Change:

- Refine `refresh_memory()` semantics so that `_base_sys_prompt` carries only stable prompt layers.
- Split long-term and daily memory access responsibilities.

Target outcome:

- long-term memory may refresh base prompt
- daily memory must not be written back into `_base_sys_prompt`

### 8.3 `src/agent_core/presenter.py`

Change:

- Make this the primary per-turn assembly point for:
  - session context
  - daily memory block
  - MCP awareness summary
  - MCP readiness summary

Reason:

- this file already owns per-turn orchestration and should remain the channel-neutral assembly surface

### 8.4 `src/agent_core/engine/messages.py`

Keep ownership of:

- history assembly
- similar flow retrieval summaries
- session image index

Do not overload this file with long-term memory semantics.

### 8.5 `src/agent_core/engine/agent.py`

Change:

- Extend the pre-LLM message assembly to include MCP awareness blocks alongside the existing skill awareness block.
- Preserve current tool schema execution semantics.

### 8.6 `src/agent_core/mcp/config.py`

Change:

- Support optional human-facing MCP metadata in config, such as:
  - `title`
  - `description`
  - `use_cases`
  - `notes`

This metadata is for awareness, not execution.

### 8.7 `src/agent_core/mcp/manager.py`

Change:

- Add a lightweight summary builder for model-facing MCP awareness and runtime status.

Expected output shape:

- server identity
- short description
- status
- tool count if known
- short last-error summary if relevant

This summary must stay concise and safe for prompt injection.

### 8.8 Memory Storage Layout

Add:

- `INSTANCE_DIR/memory.daily/`

Keep:

- `INSTANCE_DIR/memory.md`

No DB migration is required in this phase.

---

## 9. Data Model Table

| Layer | Purpose | Input | Output | Storage | Refresh Timing | In Base Prompt | Per-turn Injection |
|---|---|---|---|---|---|---|---|
| Identity | agent identity and rules | prompt files + session/channel context | identity block | files + runtime context | startup + per turn | partial | yes |
| Long-term memory | stable facts | `memory.md` | long-term memory block | file | startup / explicit write | yes | no |
| Daily memory | today's working memory | `memory.daily/YYYY-MM-DD.md` | daily memory block | file | per turn | no | yes |
| Skill awareness | available skills | skill registry | skill index | memory | per turn | no | yes |
| MCP awareness | enabled MCP description | config + runtime snapshot | capability index | config + memory snapshot | per turn | no | yes |
| Tool execution | callable tools | runtime registry | tool schemas | memory | per LLM call | no | not prompt, tools only |
| Runtime state | readiness and errors | MCP manager cache | status summary | memory | background + per turn read | no | yes |
| Core loop | orchestrate everything | user input + all layers | final messages and tools | runtime | per turn | n/a | n/a |

---

## 10. Risks

### 10.1 Daily Memory Bloat

If daily memory becomes a raw event log, it will recreate the original prompt pollution problem.

Mitigation:

- keep daily memory short and conclusion-oriented
- prefer decisions, status checkpoints, and continuation notes over raw logs

### 10.2 False Capability Confidence

If prompt awareness says an MCP exists but the model interprets that as callable, it may hallucinate tool availability.

Mitigation:

- explicitly inject readiness status
- preserve tool-schema-only execution rule

### 10.3 Responsibility Drift

If multiple files start injecting overlapping prompt fragments, the model becomes hard to reason about again.

Mitigation:

- keep stable prompt assembly in `run.py`
- keep per-turn orchestration in `presenter.py`
- keep history/image augmentation in `engine/messages.py`
- keep MCP runtime state in `mcp/manager.py`

---

## 11. Rollout Strategy

Phase 1:

- Split long-term vs daily memory
- Keep `memory.md`
- Add `memory.daily/`
- Inject only today's daily block per turn

Phase 2:

- Add MCP awareness block
- Add MCP readiness summary block
- Leave execution model unchanged

Phase 3:

- Route memory writes to correct target store
- tighten prompt size controls for daily memory if needed

---

## 12. Acceptance Criteria

The design is considered successful when all of the following are true:

- Base prompt no longer contains same-day working memory.
- The model can see today's short-term working memory separately from long-term memory.
- The model can see enabled MCP servers even before they are ready.
- The model can distinguish:
  - enabled MCP
  - known MCP
  - ready MCP
  - callable MCP
- Runtime execution still only uses schemas returned by `get_available_schemas()`.

---

## 13. Out of Scope for This Spec

- auto-compaction of daily memory
- cross-day retrieval and summarization
- MCP readiness blocking policy redesign
- replacing markdown memory with structured storage
- changing skill discovery semantics

---

## 14. Summary

This design introduces one core principle:

> Separate stable identity, stable memory, daily working memory, capability awareness, runtime readiness, and execution availability.

With this split:

- memory becomes less noisy
- MCP becomes less blind
- the Agent gains awareness before readiness
- execution remains safe by preserving schema-based tool gating
