# NanoGhost Memory System v3 Spec

> Version: v3 - 2026-05-30
> Status: Active Design
> This document replaces all previous docs/memory-*.md as the single source of truth.

---

## 1. System Overview

### 1.1 Three Stores, One Pattern

Three storage modules form the memory system. All three follow the same query pattern:

```
Layer 1 (Index):   Show available categories/sections
Layer 2 (Locate):  Focus on one area -> get summary-level content
Layer 3 (Detail):  Expand a specific record -> get full content
```

| Module | What it stores | Write method | Query interface |
|--------|---------------|-------------|----------------|
| Card   | Complete flow records (intent + steps + experience) | auto-write via record_successful_flow() + LLM summary | retrieve_similar_flows() -> get_card_detail() |
| Graph  | Step transition stats (L1~L4 multi-layer edges) | auto-cut from Card.steps | memory_explore('node') -> memory_explore('drill') |
| memory.md | daily_log + decisions | memory_write tool (Agent decides) | memory_read('index') -> memory_read('section') -> memory_read('detail') |

### 1.2 Post-turn Trigger Sequence

```
Turn ends (has tool calls + has LLM reply)
  |
  +-- [Phase 1] Write Card (record_successful_flow)
  |     +-- hash steps -> flow_hash
  |     +-- write intent + slim_steps + embedding
  |     +-- update success_count (merge same hash)
  |
  +-- [Phase 2] Write Graph (update_graph_from_steps)
  |     +-- iterate adjacent step pairs from Card.steps
  |     +-- classify() each step -> OpCode
  |     +-- for level in [1,2,3,4]: upsert edge, count += 1
  |
  +-- [Phase 3] LLM summarize experience (call ONCE, not per-step)
  |     Input: intent_summary + steps_summary + reply
  |     Output: experience text, or empty string
  |     Dedup -> card.experience_notes.append()
  |
  +-- [Phase 4] Agent writes memory.md via memory_write tool
        (Agent decides timing and content)
```

### 1.3 Key Design Decisions

- No pitfall concept. Experience summary alone covers what to note.
- No scoring/recommendation on Graph. Only raw counts.
- No content auto-extraction on memory.md. No regex, no keyword matching.
- LLM calls LLM ONCE per flow for experience. Not per-step.
- All queries are through built-in Tools. No automatic injection except memory.md section index.
---

## 2. Card Module (Flow Records)

### 2.1 Data Model

```python
@dataclass
class AgentMemoryCard:
    id: str
    flow_hash: str           # SHA256(steps)[:16]
    intent_summary: str      # user intent (for embedding search)
    intent_vector: List[float]  # embedding
    steps: List[Dict]        # [{method, path, ok, status_code}, ...]
    success_count: int
    total_rounds: int
    experience_notes: List[str]  # LLM-generated experience (no pitfalls)
    l1_code: int = 0         # main domain code, for category queries
    namespace: Optional[str] = None
```

Changes from v2:
- Removed: pitfalls, intent_examples, flow_signature fields
- Added: l1_code (main domain category)
- Old data with pitfalls field is kept in DB but not read

### 2.2 Write Rules (Post-turn)

```python
def record_successful_flow(user_intent, steps, rounds_used, db, llm, namespace):
    # 1. Validate input
    if not user_intent or not steps:
        return None
    slim_steps = _slim_steps(steps)
    if not any(s.get('ok', True) for s in slim_steps):
        return None  # all failed, skip

    # 2. Generate flow_hash from step signature
    sig = '|'.join(f"{s['method']} {s['path']}" for s in slim_steps)
    flow_hash = hashlib.sha256(sig.encode()).hexdigest()[:16]

    # 3. Load or create card
    card = _load_or_create(db, flow_hash, namespace)
    card.intent_summary = user_intent
    card.steps = slim_steps
    card.success_count += 1
    card.total_rounds += rounds_used

    # 4. Update embedding (first time or when intent changes)
    if not card.intent_vector:
        card.intent_vector = get_embedding(user_intent, llm)

    # 5. Update L1 category
    l1_codes = [classify(s).l1 for s in steps]
    card.l1_code = max(set(l1_codes), key=l1_codes.count)

    card.updated_at = time.time()
    _save_card(card, db, namespace)
    return flow_hash
```

### 2.3 Experience Summary (Phase 3)

Timing: AFTER record_successful_flow() and update_graph_from_steps() return.
Called ONCE per turn. Not per-step, not per-tool-call.

```python
def enrich_card_experience(card, reply, llm):
    steps_summary = " -> ".join(
        f"{s.get('method','')} {s.get('path','')}" for s in (card.steps or [])
    )[:300]

    prompt = f"""You just completed this task:
Intent: {card.intent_summary}
Steps: {steps_summary}
Your reply: {reply}

Summarize the experience in one paragraph (pitfalls, tips, standard procedure).
If nothing notable, output nothing."""

    resp = llm.chat(...)
    text = resp.content.strip() if resp else ''
    return text if text else None
```

### 2.4 Query Rules (Current)

```python
def retrieve_similar_flows(user_intent, top_k=3, db, llm, namespace):
    # Embedding + cosine + MMR (existing logic, unchanged)
    # Returns cards with: intent_summary, steps summary, experience_notes
    pass
```

### 2.5 Query Rules (Future - Layered Disclosure)

```
Interface 1: list_cards(domain=None) -> List[Dict]
  Returns card index (flow_hash, intent_summary, l1_code, success_count)
  Optional domain filter for category browsing

Interface 2: retrieve_similar_flows(intent, domain=None)
  Semantic search, with optional L1 domain filter

Interface 3: get_card_detail(flow_hash) -> Card
  Returns full card content (all steps + experience_notes)
```
---

## 3. Graph Module (Step Transitions)

### 3.1 Core Principle

Graph does NOT:
- Score/sort/recommend which edge is better
- Calculate transition probability
- Filter out unimportant edges
- Distinguish relation types (no FOLLOWS/DEPENDS_ON)

Graph ONLY does:
- Record: from node A, what nodes have been visited next, and how many times
- List ALL outgoing edges when queried, with raw counts
- LLM uses intent + context to decide which edge (or none)

### 3.2 Data Model

```sql
CREATE TABLE agent_edges_ml (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level INTEGER NOT NULL,
    from_code INTEGER NOT NULL,
    to_code INTEGER NOT NULL,
    total_count INTEGER DEFAULT 0,
    namespace TEXT DEFAULT '',
    created_at REAL,
    updated_at REAL,
    UNIQUE(level, from_code, to_code, namespace)
);
```

Changes from v2 graph.py:
- Removed: approved_count, approved_ratio
- Removed: relation_type (FOLLOWS/DEPENDS_ON)
- Removed: from_method, from_path, to_method, to_path (stored in code form)
- Removed: _score(), _prune_edges(), _detect_dependency()
- Added: level field for multi-layer queries

Old agent_memory_edges table kept but not written to. Migration optional.

### 3.3 OpCode (Multi-layer Encoding)

```python
@dataclass
class OpCode:
    l1: int   # domain (8bit) - hash(protocol + server)
    l2: int   # action (8bit) - hash(tool_name)
    l3: int   # resource (32bit) - hash(tool_name + path_pattern)
    l4: int   # detail (16bit) - hash(tool_name + full_path)

    def level_code(self, level: int) -> int:
        masks = {
            1: self.l1,
            2: (self.l1 << 8) | self.l2,
            3: (self.l1 << 40) | (self.l2 << 32) | self.l3,
            4: (self.l1 << 72) | (self.l2 << 64) | (self.l3 << 32) | self.l4,
        }
        return masks.get(level, self.l1)
```

### 3.4 Classifier

```python
def classify(step: Dict) -> OpCode:
    method = (step.get('method') or 'GET').upper()
    path = (step.get('path') or '').strip()
    tool_name = step.get('tool_name') or method

    # MCP tools: mcp__server_id__tool_name
    if tool_name.startswith('mcp__'):
        parts = tool_name.split('__')
        l1_source = f'mcp__{parts[1]}'
        l2_source = tool_name
    else:
        l1_source = method
        l2_source = tool_name

    l3_source = tool_name + _normalize_path(path)
    l4_source = tool_name + path

    return OpCode(
        l1=xxhash32(l1_source),
        l2=xxhash32(l2_source),
        l3=xxhash32(l3_source),
        l4=xxhash32(l4_source),
    )


def _normalize_path(path: str) -> str:
    """Replace UUIDs with placeholders for aggregation"""
    p = (path or '').split('?', 1)[0]
    uuid_pat = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    return re.sub(uuid_pat, '{id}', p)
```

### 3.5 Write Rules

```python
def update_graph_from_steps(steps, db, namespace=None):
    if not steps or len(steps) < 2:
        return
    now = time.time()
    for i in range(len(steps) - 1):
        a = classify(steps[i])
        b = classify(steps[i + 1])
        for level in [1, 2, 3, 4]:
            _upsert_edge(level, a.level_code(level), b.level_code(level), db, namespace)
```

### 3.6 Query Rules: memory_explore Tool

```
BUILT-IN TOOL: memory_explore

Query the operation memory graph. Two actions for layered disclosure.

Action 1: 'node'
  Input: method, path
  Returns: L1 domain-level + L2 action-level outgoing edges
  Data volume: L1+L2 typically < 10 items

  Example:
  > memory_explore(action='node', method='GET', path='/api/tasks')
  [L1] API -> API (8)
  [L2] GET -> POST (6), GET -> GET (3), GET -> PUT (2)

Action 2: 'drill'
  Input: from_code, to_code, level=3
  Returns: L3 resource-level detail for that specific transition
  Optional level=4 for most detailed view

  Example:
  > memory_explore(action='drill', from_code=..., to_code=..., level=3)
  [L3] GET /api/tasks -> POST /api/tasks (4)
  [L3] GET /api/tasks -> POST /api/reports (2)
```
---

## 4. memory.md Module

### 4.1 Write Rules

Agent writes via memory_write built-in tool. No auto-extraction.

```
BUILT-IN TOOL: memory_write
  action: 'append' | 'update' | 'delete'
  section: 'daily_log' | 'decisions'
  content: str
  key: Optional[str]  (for update/delete)
```

Two sections:
- daily_log: what was done today
- decisions: key conclusions, design decisions from discussions

### 4.2 Read Rules (Layered Disclosure)

```
BUILT-IN TOOL: memory_read

Action 1: 'index'
  Returns: section titles + line count per section
  Example:
  > memory_read(action='index')
  daily_log (12 lines)
  decisions (8 lines)

Action 2: 'section'
  Input: section name
  Returns: full content of that section
  Example:
  > memory_read(action='section', name='daily_log')
  2026-05-30: Fixed YAML parser bugs ...

Action 3: 'detail'
  Input: section name + keyword
  Returns: matching entries in that section
  Example:
  > memory_read(action='detail', section='decisions', keyword='MCP')
  - Classifier should not rely on tool self-declared labels
```

### 4.3 Injection Strategy

| Turn | What is injected |
|------|-----------------|
| Every round | Section index only (title + line count), ~3-5 lines |
| LLM calls memory_read | Full section content on demand |

NO full-file injection. Unscalable as data grows.

---

## 5. Layered Disclosure Summary

| Layer | Card | Graph | memory.md |
|-------|------|-------|-----------|
| 1 (Index) | list_cards(domain=None) | memory_explore('node') | memory_read('index') |
| 2 (Locate) | retrieve_similar_flows(intent, domain) | memory_explore('drill', level=3) | memory_read('section') |
| 3 (Detail) | get_card_detail(flow_hash) | memory_explore('drill', level=4) | memory_read('detail') |

All three stores expose their data through built-in tools.
No automatic injection except memory.md section index (small, ~3-5 lines).

---

## 6. LLM Involvement Summary

| Node | Trigger | Input | Output | Frequency |
|------|---------|-------|--------|-----------|
| Card experience | All tool calls done | intent + steps + reply | experience text or empty | Once per flow |
| memory.md write | Agent judges | conversation context | daily_log / decisions | Agent decides |
| Graph query | LLM calls tool | current node | outgoing edge list | LLM decides |

LLM does NOT:
- Score/recommend Graph edges
- Detect per-step failures (no pitfall)
- Auto-extract memory.md content
- Judge whether experience is worth recording (the prompt handles this)

---

## 7. Implementation Order

| Phase | Change | Files | Lines |
|:-----:|--------|-------|:-----:|
| 1 | Graph: remove scoring, pruning, approved_count, relation_type | graph.py | -100 |
| 2 | Card: remove pitfalls, change experience to per-flow trigger | cards.py | -80 |
| 3 | Implement classify() + OpCode + agent_edges_ml table | classifier.py (new) + graph.py | +150 |
| 4 | Rewrite update_graph_from_steps() for multi-layer writes | graph.py | +60 |
| 5 | memory_explore tool (node + drill) | tool/builtins.py or memory/ | +80 |
| 6 | memory_read tool (index + section + detail) + injection change | tool/builtins.py + agent.py | +80 |
| 7 | Card layering: list_cards() + get_card_detail() | cards.py | +50 |

Phase 1+2: pure deletions, safe and low-risk. Do first.
Phase 3+4: necessary infra (multi-layer Graph). Required before tools work.
Phase 5+6+7: optional query layer. Add as needed.
