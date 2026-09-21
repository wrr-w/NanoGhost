# Single Router Unified Route Envelope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前双入口的入站/出站链路收口为单 `Router` + 单 `RouteEnvelope` 模型，并保持现有飞书主链路与回复能力不回归。

**Architecture:** 新增统一模型 `RouteEnvelope` 和 `Router.submit()` 作为唯一正式入口，先通过兼容层接管入站与出站，再把飞书、调度器、子任务池和 `TurnResponder` 切到新主路径。`InboxHub` 继续作为入站下游队列，`Channel` / `ChannelIO` 继续作为出站下游投递层，不在本轮重写。

**Tech Stack:** Python 3.13, pytest, dataclasses, existing `Router` / `InboxHub` / `Channel` abstractions

---

### Task 1: 定义统一路由模型与 Router 主入口

**Files:**
- Create: `src/agent_core/channel/route.py`
- Modify: `src/agent_core/router.py`
- Test: `tests/test_router_route_envelope.py`

- [ ] **Step 1: 写失败测试，锁定 `RouteEnvelope` 和 `Router.submit()` 的基本行为**

```python
from agent_core.channel.route import RouteEnvelope
from agent_core.router import Router


def test_route_envelope_defaults_are_stable():
    env = RouteEnvelope(direction="inbound", kind="channel_message", target_addr="feishu:oc_x")
    assert env.direction == "inbound"
    assert env.kind == "channel_message"
    assert env.target_addr == "feishu:oc_x"
    assert env.to == []
    assert env.images == []
    assert env.meta == {}


def test_router_submit_rejects_unknown_direction():
    router = Router()
    rep = router.submit(RouteEnvelope(direction="sideways", kind="event", target_addr="x"))
    assert rep["ok"] is False
    assert "direction" in rep["error"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_router_route_envelope.py -v`
Expected: FAIL with `ModuleNotFoundError` or `AttributeError` for missing `RouteEnvelope` / `submit`

- [ ] **Step 3: 写最小实现**

```python
# src/agent_core/channel/route.py
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RouteEnvelope:
    direction: str
    kind: str
    source_addr: str = ""
    target_addr: str = ""
    to: list[str] = field(default_factory=list)
    payload: Any = None
    text: str = ""
    images: list[str] = field(default_factory=list)
    reply_to: str | None = None
    reaction: dict[str, Any] | None = None
    agent_key: str = "default"
    summary: str = ""
    route_key: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
```

```python
# src/agent_core/router.py
from agent_core.channel.route import RouteEnvelope

def submit(self, envelope: RouteEnvelope):
    env = self.normalize(envelope)
    if env.direction == "inbound":
        return self.route_inbound(env)
    if env.direction == "outbound":
        return self.route_outbound(env)
    return {"ok": False, "error": f"invalid direction: {env.direction}"}

def normalize(self, envelope: RouteEnvelope) -> RouteEnvelope:
    envelope.direction = (envelope.direction or "").strip().lower()
    envelope.kind = (envelope.kind or "event").strip()
    envelope.source_addr = (envelope.source_addr or "").strip()
    envelope.target_addr = (envelope.target_addr or "").strip()
    envelope.to = list(envelope.to or [])
    envelope.images = list(envelope.images or [])
    envelope.meta = dict(envelope.meta or {})
    return envelope
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_router_route_envelope.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/channel/route.py src/agent_core/router.py tests/test_router_route_envelope.py
git commit -m "feat: add route envelope and router submit entry"
```

### Task 2: 把入站收口到 Router 内部

**Files:**
- Modify: `src/agent_core/router.py`
- Modify: `src/agent_core/channel/inbound.py`
- Modify: `src/agent_core/runtime/inbox.py`
- Test: `tests/test_router_route_envelope.py`
- Test: `tests/test_inbound_dispatcher.py`

- [ ] **Step 1: 写失败测试，锁定入站 envelope 到 `InboxEvent` 的映射**

```python
from agent_core.channel.route import RouteEnvelope
from agent_core.runtime.inbox import InboxHub
from agent_core.router import Router


def test_router_submit_inbound_adapts_to_inbox_event():
    hub = InboxHub()
    router = Router(hub=hub)
    router.submit(
        RouteEnvelope(
            direction="inbound",
            kind="timer",
            target_addr="feishu:sched:daily",
            source_addr="timer",
            payload={"task": "daily"},
            summary="定时任务 daily",
        )
    )
    ev = hub.drain("feishu:sched:daily")[0]
    assert ev.kind == "timer"
    assert ev.source == "timer"
    assert ev.summary == "定时任务 daily"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_router_route_envelope.py::test_router_submit_inbound_adapts_to_inbox_event -v`
Expected: FAIL with missing `route_inbound` / `adapt_to_inbox`

- [ ] **Step 3: 写最小实现**

```python
# src/agent_core/router.py
from agent_core.runtime.inbox import InboxEvent, get_hub

def __init__(self, registry=None, *, manager=None, mirror=None, hub=None) -> None:
    ...
    self.hub = hub or get_hub()

def route_inbound(self, envelope):
    if not envelope.target_addr:
        return {"ok": False, "error": "missing target_addr"}
    inbox = self.adapt_to_inbox(envelope)
    self.hub.submit(inbox)
    self._audit(envelope.target_addr, envelope.summary or envelope.kind, "inbound", envelope.agent_key)
    return inbox

def adapt_to_inbox(self, envelope):
    return InboxEvent(
        target=envelope.target_addr,
        kind=envelope.kind,
        payload=envelope.payload,
        source=envelope.source_addr or str(envelope.meta.get("source", "") or ""),
        summary=envelope.summary,
    )
```

```python
# src/agent_core/channel/inbound.py
from agent_core.channel.route import RouteEnvelope
from agent_core.router import get_router

def submit_unified(self, event: UnifiedInboundEvent):
    return get_router().submit(
        RouteEnvelope(
            direction="inbound",
            kind=event.kind,
            target_addr=event.target,
            source_addr=event.source,
            payload=event.payload,
            summary=event.summary,
            route_key=event.route_key,
            meta=dict(event.meta or {}),
        )
    )
```

- [ ] **Step 4: 跑入站测试确认通过**

Run: `pytest tests/test_router_route_envelope.py::test_router_submit_inbound_adapts_to_inbox_event tests/test_inbound_dispatcher.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/router.py src/agent_core/channel/inbound.py tests/test_router_route_envelope.py tests/test_inbound_dispatcher.py
git commit -m "feat: route inbound envelopes through router"
```

### Task 3: 迁移飞书、调度器、子任务池到 Router.submit(inbound)

**Files:**
- Modify: `src/agent_core/channel/feishu/ws_client.py`
- Modify: `src/agent_core/scheduler.py`
- Modify: `src/agent_core/runtime/subagent_pool.py`
- Test: `tests/test_inbound_dispatcher.py`

- [ ] **Step 1: 写失败测试，锁定三个入口不再依赖 dispatcher 主入口**

```python
from agent_core.channel.route import RouteEnvelope


def test_inbound_dispatcher_still_routes_via_router(monkeypatch):
    seen = []

    class _FakeRouter:
        def submit(self, env):
            seen.append(env)
            return {"ok": True}

    monkeypatch.setattr("agent_core.channel.inbound.get_router", lambda: _FakeRouter())
    dispatcher = InboundDispatcher(hub=InboxHub())
    dispatcher.submit_timer(target="feishu:sched:daily", payload={"task": "x"}, summary="定时任务 daily")
    assert isinstance(seen[0], RouteEnvelope)
    assert seen[0].direction == "inbound"
    assert seen[0].kind == "timer"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_inbound_dispatcher.py::test_inbound_dispatcher_still_routes_via_router -v`
Expected: FAIL if dispatcher still directly returns `InboxEvent`

- [ ] **Step 3: 迁移三个入口**

```python
# src/agent_core/channel/feishu/ws_client.py
from agent_core.channel.route import RouteEnvelope
from agent_core.router import get_router

get_router().submit(
    RouteEnvelope(
        direction="inbound",
        kind="channel_message",
        target_addr=f"feishu:{chat_id}",
        source_addr="feishu",
        payload=event_data,
        summary=f"{chat_type} {sender_open_id}",
    )
)
```

```python
# src/agent_core/scheduler.py
get_router().submit(
    RouteEnvelope(
        direction="inbound",
        kind="timer",
        target_addr=f"feishu:{t.session_key}",
        source_addr="timer",
        payload={"task": t},
        summary=f"定时任务 {t.name}",
    )
)
```

```python
# src/agent_core/runtime/subagent_pool.py
get_router().submit(
    RouteEnvelope(
        direction="inbound",
        kind="subagent_done",
        target_addr=rec["target"],
        source_addr="subagent",
        payload={
            "run_id": run_id,
            "description": rec["description"],
            "status": rec["status"],
            "result": result,
            "error": error,
        },
        summary=f"子任务 {rec['description']} {rec['status']}",
    )
)
```

- [ ] **Step 4: 跑针对性测试**

Run: `pytest tests/test_inbound_dispatcher.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/channel/feishu/ws_client.py src/agent_core/scheduler.py src/agent_core/runtime/subagent_pool.py tests/test_inbound_dispatcher.py
git commit -m "feat: route inbound sources through router submit"
```

### Task 4: 把出站主路径迁移到 RouteEnvelope

**Files:**
- Modify: `src/agent_core/channel/responder.py`
- Modify: `src/agent_core/router.py`
- Modify: `src/agent_core/presenter.py`
- Test: `tests/test_presenter_router.py`
- Test: `tests/test_router_outbound_envelope.py`
- Test: `tests/test_router_route_envelope.py`

- [ ] **Step 1: 写失败测试，锁定 `TurnResponder` 产出的已是 `RouteEnvelope`**

```python
from agent_core.channel.route import RouteEnvelope
from agent_core.channel.responder import TurnResponder


def test_turn_responder_reply_current_creates_outbound_route_envelope():
    class FakeRouter:
        def __init__(self):
            self.envelopes = []
        def submit(self, envelope):
            self.envelopes.append(envelope)
            return {"ok": True}

    router = FakeRouter()
    responder = TurnResponder(router=router, platform="feishu", chat_id="oc_x", message_id="om_1")
    responder.reply_current("done")
    env = router.envelopes[0]
    assert isinstance(env, RouteEnvelope)
    assert env.direction == "outbound"
    assert env.reply_to == "om_1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_presenter_router.py::test_turn_responder_reply_current_creates_outbound_route_envelope -v`
Expected: FAIL while responder still builds `OutboundEnvelope`

- [ ] **Step 3: 写最小实现并保留兼容层**

```python
# src/agent_core/channel/responder.py
from agent_core.channel.route import RouteEnvelope

def _base_envelope(self, text: str = "") -> RouteEnvelope:
    return RouteEnvelope(
        direction="outbound",
        kind="text",
        to=[f"{self.platform}:{self.chat_id}"],
        target_addr=f"{self.platform}:{self.chat_id}",
        source_addr=f"{self.platform}:{self.chat_id}",
        text=text,
        agent_key=self.namespace,
    )

def reply_current(self, text: str) -> bool:
    env = self._base_envelope(text)
    env.reply_to = self.message_id or None
    return bool(self.router.submit(env).get("ok"))
```

```python
# src/agent_core/router.py
def route_outbound(self, envelope):
    return self.deliver_outbound(envelope)

def deliver_outbound(self, envelope):
    targets = self.resolve(envelope.to or envelope.target_addr, {"source_addr": envelope.source_addr})
    ...

def send_envelope(self, envelope, *, hop=0):
    return self.submit(
        RouteEnvelope(
            direction="outbound",
            kind="image" if getattr(envelope, "images", None) else "text",
            to=list(getattr(envelope, "to", []) or []),
            target_addr=(getattr(envelope, "to", [""])[0] if getattr(envelope, "to", None) else ""),
            source_addr=getattr(envelope, "source_addr", ""),
            text=getattr(envelope, "text", ""),
            images=list(getattr(envelope, "images", []) or []),
            reply_to=getattr(envelope, "reply_to", None),
            reaction=getattr(envelope, "reaction", None),
            agent_key=getattr(envelope, "agent_key", "default"),
            meta=dict(getattr(envelope, "meta", {}) or {}),
        )
    )
```

- [ ] **Step 4: 跑出站测试**

Run: `pytest tests/test_presenter_router.py tests/test_router_outbound_envelope.py tests/test_router_route_envelope.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/channel/responder.py src/agent_core/router.py src/agent_core/presenter.py tests/test_presenter_router.py tests/test_router_outbound_envelope.py tests/test_router_route_envelope.py
git commit -m "feat: route outbound messages through route envelope"
```

### Task 5: 清理旧入口地位并更新文档

**Files:**
- Modify: `src/agent_core/channel/inbound.py`
- Modify: `docs/current-architecture-state.md`
- Modify: `docs/channels_and_router.md`
- Modify: `src/agent_core/channel/interfaces.py`
- Test: `tests/test_inbound_dispatcher.py`
- Test: `tests/test_router_route_envelope.py`

- [ ] **Step 1: 写失败测试，锁定 `InboundDispatcher` 只是兼容壳**

```python
def test_dispatcher_submit_unified_delegates_to_router_submit(monkeypatch):
    calls = []

    class _FakeRouter:
        def submit(self, env):
            calls.append(env)
            return {"ok": True}

    monkeypatch.setattr("agent_core.channel.inbound.get_router", lambda: _FakeRouter())
    dispatcher = InboundDispatcher(hub=InboxHub())
    dispatcher.submit_unified(
        UnifiedInboundEvent(
            target="feishu:oc_z",
            kind="channel_message",
            payload={"raw": True},
            source="feishu",
            summary="raw event",
        )
    )
    assert len(calls) == 1
    assert calls[0].direction == "inbound"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_inbound_dispatcher.py::test_dispatcher_submit_unified_delegates_to_router_submit -v`
Expected: FAIL if dispatcher still owns the main path

- [ ] **Step 3: 更新文档与注释**

```markdown
# docs/current-architecture-state.md
- 统一主入口：`RouteEnvelope -> Router.submit(...)`
- 入站下游：`Router -> InboxEvent -> InboxHub -> ResidentConsumer`
- 出站下游：`Router -> Channel -> ChannelIO`
```

```python
# src/agent_core/channel/interfaces.py
"""
当前实装边界：
  · 所有入站/出站统一走 Router
  · InboxHub 是 Router 的入站下游
  · ChannelIO 只负责平台 API 细节
"""
```

- [ ] **Step 4: 跑回归**

Run: `pytest -q tests/test_router_route_envelope.py tests/test_inbound_dispatcher.py tests/test_presenter_router.py tests/test_router_outbound_envelope.py tests/test_memory_pipeline.py tests/test_channel_p5.py`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/channel/inbound.py docs/current-architecture-state.md docs/channels_and_router.md src/agent_core/channel/interfaces.py tests/test_inbound_dispatcher.py tests/test_router_route_envelope.py
git commit -m "refactor: document single router as unified route entry"
```

### Task 6: 做仓库级验证并记录已知非本轮失败

**Files:**
- Modify: `docs/current-architecture-state.md`

- [ ] **Step 1: 跑扩展回归**

Run: `pytest -q tests/test_router_route_envelope.py tests/test_inbound_dispatcher.py tests/test_presenter_router.py tests/test_router_outbound_envelope.py tests/test_memory_pipeline.py tests/test_channel_p5.py`
Expected: PASS

- [ ] **Step 2: 跑排除私有环境依赖后的全量回归**

Run: `pytest -q --ignore=tests/test_reaction.py --ignore=tests/test_reaction_api.py`
Expected: 主链通过；若仍有 MCP client 历史失败，记录为非本轮问题

- [ ] **Step 3: 在架构状态文档补一句测试结论**

```markdown
## 回归状态

- 单 Router 主链相关测试通过
- `reaction` 环境依赖测试未纳入本轮
- 若 `MCP client` 历史测试仍失败，视为本轮外问题
```

- [ ] **Step 4: 提交**

```bash
git add docs/current-architecture-state.md
git commit -m "test: record single router regression status"
```
