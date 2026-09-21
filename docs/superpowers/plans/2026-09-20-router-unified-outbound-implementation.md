# Router Unified Outbound Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前会话回复与主动外发统一收口到 `Router`，并保留 `reply_to` 语义与 `Channel default + Endpoint override` 规则。

**Architecture:** 引入 `OutboundEnvelope` 作为统一出站模型，让 `Router` 成为唯一正式出站入口。`TurnResponder` 从直接执行 `ChannelIO` 调用改为只封装 envelope 并调用 `Router`；`ChannelIO` 下沉为 `Channel` 内部实现细节。

**Tech Stack:** Python 3.x、`asyncio`、`pytest`、现有 `Router` / `Channel` / `ChannelIO` 抽象、Feishu 通道实现。

---

## 文件结构与职责锁定

### 新建文件

- `src/agent_core/channel/outbound.py`
  - 统一定义 `OutboundEnvelope` 与 policy 解析 helper
- `tests/test_router_outbound_envelope.py`
  - 覆盖 envelope、reply policy、reply/send 退化逻辑

### 修改文件

- `src/agent_core/channel/base.py`
  - 为 `Channel` 增加默认发送策略接口
- `src/agent_core/channel/endpoint.py`
  - 明确 endpoint reply policy 元数据约定
- `src/agent_core/channel/manager.py`
  - 提供 policy 读取与 merge helper
- `src/agent_core/channel/feishu/channel.py`
  - 声明飞书 channel 的默认 reply 策略
- `src/agent_core/router.py`
  - 增加 `send_envelope()` / `deliver_envelope()`，并用 policy 决定 `reply` 或 `send`
- `src/agent_core/channel/responder.py`
  - 删除对 `ChannelIO` 的直接依赖，改为构造 envelope 并调用 router
- `src/agent_core/presenter.py`
  - 不再传入 `ChannelIO` 给 `TurnResponder`，仅传 router 和当前 source 元数据
- `tests/test_presenter_router.py`
  - 改为断言 responder 构造 envelope 并调用 router
- `tests/test_channel_p5.py`
  - 扩展 Router 的 reply policy 场景

---

### Task 1: 定义 OutboundEnvelope 与 reply policy 合并规则

**Files:**
- Create: `src/agent_core/channel/outbound.py`
- Modify: `src/agent_core/channel/base.py`
- Modify: `src/agent_core/channel/endpoint.py`
- Modify: `src/agent_core/channel/manager.py`
- Test: `tests/test_router_outbound_envelope.py`

- [ ] **Step 1: 写失败测试，锁定 envelope 与 policy merge**

```python
from agent_core.channel.endpoint import Endpoint
from agent_core.channel.outbound import OutboundEnvelope, resolve_reply_policy


def test_resolve_reply_policy_endpoint_overrides_channel_default():
    ep = Endpoint(
        addr="feishu:oc_1",
        channel="feishu",
        meta={"allow_reply": False, "prefer_reply": False},
    )
    policy = resolve_reply_policy(
        endpoint=ep,
        channel_policy={
            "supports_reply": True,
            "default_allow_reply": True,
            "default_prefer_reply": True,
        },
    )

    assert policy["supports_reply"] is True
    assert policy["allow_reply"] is False
    assert policy["prefer_reply"] is False


def test_outbound_envelope_defaults_are_stable():
    env = OutboundEnvelope(to=["feishu:oc_1"], text="hi")

    assert env.to == ["feishu:oc_1"]
    assert env.text == "hi"
    assert env.reply_to is None
    assert env.images == []
```

- [ ] **Step 2: 运行测试，确认当前不存在 envelope 与 helper**

Run: `pytest tests/test_router_outbound_envelope.py -q`
Expected: FAIL，提示 `ModuleNotFoundError: No module named 'agent_core.channel.outbound'`

- [ ] **Step 3: 写最小实现，增加 envelope 与 policy helper**

```python
# src/agent_core/channel/outbound.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class OutboundEnvelope:
    to: List[str]
    text: str = ""
    source_addr: str = ""
    agent_key: str = "default"
    reply_to: Optional[str] = None
    images: List[str] = field(default_factory=list)
    update_of: Optional[str] = None
    reaction: Optional[Dict[str, Any]] = None
    meta: Dict[str, Any] = field(default_factory=dict)


def resolve_reply_policy(*, endpoint, channel_policy: Dict[str, Any]) -> Dict[str, bool]:
    meta = dict(getattr(endpoint, "meta", {}) or {})
    supports_reply = bool(channel_policy.get("supports_reply", False))
    allow_reply = meta["allow_reply"] if "allow_reply" in meta else bool(channel_policy.get("default_allow_reply", False))
    prefer_reply = meta["prefer_reply"] if "prefer_reply" in meta else bool(channel_policy.get("default_prefer_reply", False))
    return {
        "supports_reply": supports_reply,
        "allow_reply": bool(allow_reply),
        "prefer_reply": bool(prefer_reply),
    }
```

```python
# src/agent_core/channel/base.py
def default_delivery_policy(self) -> dict:
    return {
        "supports_reply": True,
        "default_allow_reply": False,
        "default_prefer_reply": False,
    }
```

- [ ] **Step 4: 在 manager 中补 policy 读取 helper**

```python
# src/agent_core/channel/manager.py
def resolve_delivery_policy(self, addr: str, channel) -> dict:
    from .outbound import resolve_reply_policy

    ep = self.get_endpoint(addr)
    return resolve_reply_policy(
        endpoint=ep,
        channel_policy=(channel.default_delivery_policy() if channel else {}),
    )
```

Run: `pytest tests/test_router_outbound_envelope.py tests/test_channel_p5.py -q`
Expected: PASS，且现有 manager 测试不回归

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/channel/outbound.py src/agent_core/channel/base.py src/agent_core/channel/endpoint.py src/agent_core/channel/manager.py tests/test_router_outbound_envelope.py
git commit -m "feat: add outbound envelope and reply policy helpers"
```

### Task 2: 扩展 Router 为统一 envelope 出站入口

**Files:**
- Modify: `src/agent_core/router.py`
- Modify: `src/agent_core/channel/feishu/channel.py`
- Test: `tests/test_router_outbound_envelope.py`
- Test: `tests/test_channel_p5.py`

- [ ] **Step 1: 写失败测试，锁定 reply/send 选择逻辑**

```python
from agent_core.channel.base import Channel
from agent_core.channel.manager import ChannelManager
from agent_core.channel.outbound import OutboundEnvelope
from agent_core.channel.registry import ChannelRegistry
from agent_core.channel.directory import EndpointDirectory
from agent_core.router import Router


class FakeReplyChannel(Channel):
    name = "fake"

    def __init__(self):
        self.sent = []
        self.replied = []

    def send(self, target: str, text: str) -> bool:
        self.sent.append((target, text))
        return True

    def reply(self, message_id: str, text: str) -> bool:
        self.replied.append((message_id, text))
        return True

    def default_delivery_policy(self) -> dict:
        return {
            "supports_reply": True,
            "default_allow_reply": True,
            "default_prefer_reply": True,
        }


def test_router_send_envelope_prefers_reply_when_policy_allows():
    mgr = ChannelManager(registry=ChannelRegistry(), directory=EndpointDirectory())
    ch = FakeReplyChannel()
    mgr.register_channel(ch)
    mgr.register_endpoint("fake:a", channel="fake", kind="group", capabilities={"text"})
    router = Router(registry=mgr.registry, manager=mgr)

    rep = router.send_envelope(OutboundEnvelope(to=["fake:a"], text="hi", reply_to="m1"))

    assert rep["ok"] is True
    assert ch.replied == [("m1", "hi")]
    assert ch.sent == []


def test_router_send_envelope_falls_back_to_send_when_endpoint_disables_reply():
    mgr = ChannelManager(registry=ChannelRegistry(), directory=EndpointDirectory())
    ch = FakeReplyChannel()
    mgr.register_channel(ch)
    mgr.register_endpoint("fake:b", channel="fake", kind="group", capabilities={"text"}, meta={"allow_reply": False})
    router = Router(registry=mgr.registry, manager=mgr)

    rep = router.send_envelope(OutboundEnvelope(to=["fake:b"], text="hi", reply_to="m2"))

    assert rep["ok"] is True
    assert ch.replied == []
    assert ch.sent == [("b", "hi")]
```

- [ ] **Step 2: 运行测试，确认当前 Router 不支持 envelope**

Run: `pytest tests/test_router_outbound_envelope.py::test_router_send_envelope_prefers_reply_when_policy_allows -q`
Expected: FAIL，提示 `AttributeError: 'Router' object has no attribute 'send_envelope'`

- [ ] **Step 3: 写最小实现，增加 envelope 路由能力**

```python
# src/agent_core/router.py
from agent_core.channel.outbound import OutboundEnvelope


def deliver_envelope(self, envelope: OutboundEnvelope, *, hop: int = 0) -> Dict[str, Any]:
    targets = self.resolve(envelope.to, {"source_addr": envelope.source_addr})
    ...
    for addr in targets:
        channel, target = parse_addr(addr) if ":" in addr else ("", addr)
        ch = reg.get(channel) if channel else None
        policy = manager.resolve_delivery_policy(addr, ch) if ch else {}
        ...
        use_reply = bool(
            envelope.reply_to
            and policy.get("supports_reply")
            and policy.get("allow_reply")
            and policy.get("prefer_reply")
        )
        if use_reply:
            ok = bool(ch.reply(envelope.reply_to, envelope.text))
        else:
            ok = bool(ch.send(target, envelope.text))


def send_envelope(self, envelope: OutboundEnvelope, *, hop: int = 0) -> Dict[str, Any]:
    return self.deliver_envelope(envelope, hop=hop)
```

```python
# src/agent_core/channel/feishu/channel.py
def default_delivery_policy(self) -> dict:
    return {
        "supports_reply": True,
        "default_allow_reply": True,
        "default_prefer_reply": True,
    }
```

- [ ] **Step 4: 让旧 send 继续包装 envelope**

```python
# src/agent_core/router.py
def send(self, to: Any, text: str, *, ctx: Optional[Dict[str, Any]] = None, agent_key: str = "default", hop: int = 0) -> Dict[str, Any]:
    ctx = ctx or {}
    return self.send_envelope(
        OutboundEnvelope(
            to=self.resolve(to, ctx),
            text=text,
            source_addr=(ctx.get("source_addr") or ""),
            agent_key=agent_key,
        ),
        hop=hop,
    )
```

Run: `pytest tests/test_router_outbound_envelope.py tests/test_channel_p2.py tests/test_channel_p5.py -q`
Expected: PASS，且旧 send/fanout 行为不回归

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/router.py src/agent_core/channel/feishu/channel.py tests/test_router_outbound_envelope.py tests/test_channel_p5.py
git commit -m "feat: route unified outbound envelopes through router"
```

### Task 3: 改造 TurnResponder，让 Presenter 退出 ChannelIO

**Files:**
- Modify: `src/agent_core/channel/responder.py`
- Modify: `src/agent_core/presenter.py`
- Test: `tests/test_presenter_router.py`

- [ ] **Step 1: 写失败测试，锁定 TurnResponder 只调用 router**

```python
from agent_core.channel.responder import TurnResponder


class FakeRouter:
    def __init__(self):
        self.envelopes = []

    def send_envelope(self, envelope, *, hop=0):
        self.envelopes.append(envelope)
        return {"ok": True}


def test_turn_responder_reply_current_creates_reply_to_envelope():
    router = FakeRouter()
    responder = TurnResponder(router=router, platform="feishu", chat_id="oc_x", message_id="om_1")

    responder.reply_current("done")

    env = router.envelopes[0]
    assert env.to == ["feishu:oc_x"]
    assert env.text == "done"
    assert env.reply_to == "om_1"


def test_turn_responder_send_current_creates_plain_envelope():
    router = FakeRouter()
    responder = TurnResponder(router=router, platform="feishu", chat_id="oc_x")

    responder.send_current("hello")

    env = router.envelopes[0]
    assert env.to == ["feishu:oc_x"]
    assert env.text == "hello"
    assert env.reply_to is None
```

- [ ] **Step 2: 运行测试，确认当前 responder 仍依赖 io**

Run: `pytest tests/test_presenter_router.py -q`
Expected: FAIL，提示 `TypeError` 或旧断言失败

- [ ] **Step 3: 写最小实现，删除 TurnResponder 对 io 的依赖**

```python
# src/agent_core/channel/responder.py
from agent_core.channel.outbound import OutboundEnvelope


class TurnResponder:
    def __init__(self, *, router, platform: str, chat_id: str, message_id: str = "", namespace: str = "default") -> None:
        self.router = router
        self.platform = platform
        self.chat_id = chat_id
        self.message_id = message_id
        self.namespace = namespace

    def _base_envelope(self, text: str) -> OutboundEnvelope:
        return OutboundEnvelope(
            to=[f"{self.platform}:{self.chat_id}"],
            text=text,
            source_addr=f"{self.platform}:{self.chat_id}",
            agent_key=self.namespace,
        )

    def reply_current(self, text: str):
        env = self._base_envelope(text)
        env.reply_to = self.message_id or None
        return self.router.send_envelope(env)

    def send_current(self, text: str):
        return self.router.send_envelope(self._base_envelope(text))
```

- [ ] **Step 4: 在 Presenter 中去掉 io 传递，只保留 reaction 直连**

```python
# src/agent_core/presenter.py
responder = TurnResponder(
    router=get_router(),
    platform=getattr(source, "platform", "feishu"),
    chat_id=chat_id,
    message_id=message_id,
    namespace=getattr(agent, "namespace", "default") or "default",
)
```

Run: `pytest tests/test_presenter_router.py tests/test_basic.py tests/test_prompt_layering.py -q`
Expected: PASS，且 Presenter 仍能正常发当前回复

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/channel/responder.py src/agent_core/presenter.py tests/test_presenter_router.py
git commit -m "refactor: send current turn output through router envelopes"
```

### Task 4: 保持飞书能力与回归稳定

**Files:**
- Modify: `src/agent_core/channel/responder.py`
- Modify: `src/agent_core/router.py`
- Test: `tests/test_router_outbound_envelope.py`
- Test: `tests/test_memory_pipeline.py`
- Test: `tests/test_prompt_layering.py`

- [ ] **Step 1: 写失败测试，锁定 send_current / reply_current 不影响 memory/presenter 行为**

```python
def test_turn_responder_notify_uses_plain_envelope():
    router = FakeRouter()
    responder = TurnResponder(router=router, platform="feishu", chat_id="oc_x", message_id="om_1", namespace="ns")

    responder.notify(["feishu:ou_a"], "hello")

    env = router.envelopes[0]
    assert env.to == ["feishu:ou_a"]
    assert env.reply_to is None
    assert env.agent_key == "ns"
```

- [ ] **Step 2: 运行相关回归集**

Run: `pytest tests/test_router_outbound_envelope.py tests/test_presenter_router.py tests/test_memory_pipeline.py tests/test_basic.py tests/test_prompt_layering.py -q`
Expected: 若失败，应只剩 envelope/Presenter 迁移引起的问题

- [ ] **Step 3: 修正兼容点**

```python
# src/agent_core/channel/responder.py
def notify(self, to, text: str, *, namespace: str | None = None):
    env = OutboundEnvelope(
        to=list(to) if isinstance(to, (list, tuple, set)) else [str(to)],
        text=text,
        source_addr=f"{self.platform}:{self.chat_id}",
        agent_key=namespace or self.namespace,
    )
    return self.router.send_envelope(env)
```

```python
# src/agent_core/router.py
if self.mirror and addr != envelope.source_addr:
    self.mirror(addr, envelope.text)
```

- [ ] **Step 4: 跑完整回归集**

Run: `pytest tests/test_channel_p0.py tests/test_channel_p2.py tests/test_channel_p5.py tests/test_router_outbound_envelope.py tests/test_presenter_router.py tests/test_memory_pipeline.py tests/test_basic.py tests/test_prompt_layering.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/channel/responder.py src/agent_core/router.py tests/test_router_outbound_envelope.py tests/test_presenter_router.py
git commit -m "test: preserve router reply semantics and presenter regressions"
```

---

## 测试矩阵

- Envelope / Router：
  - `pytest tests/test_router_outbound_envelope.py tests/test_channel_p2.py tests/test_channel_p5.py -q`
- Presenter / 当前回复：
  - `pytest tests/test_presenter_router.py tests/test_basic.py tests/test_prompt_layering.py -q`
- 综合回归：
  - `pytest tests/test_channel_p0.py tests/test_channel_p2.py tests/test_channel_p5.py tests/test_router_outbound_envelope.py tests/test_presenter_router.py tests/test_memory_pipeline.py tests/test_basic.py tests/test_prompt_layering.py -q`

## 风险与回滚

- 当前消息回复从 `io.reply()` 切到 `Router.send_envelope(reply_to=...)` 后，若 policy 解析错误，聊天端点会退化成普通 send。
- `TurnResponder` 去掉 `io` 后，现有测试和调用点都要同步调整。
- 飞书 channel 默认 reply policy 如果漏配，会直接改变当前回复表现。
- 回滚顺序：
  - 先回滚 `presenter.py` 与 `responder.py`
  - 再回滚 `router.py` 的 envelope 模式
  - 最后回滚 `outbound.py` 与 policy merge

## 自检

- Spec 覆盖：
  - 统一出站 Router：Task 2 / Task 3
  - `reply_to`：Task 1 / Task 2 / Task 3
  - `Channel default + Endpoint override`：Task 1 / Task 2
  - 编排层退出 `ChannelIO`：Task 3
- 占位符检查：
  - 无 `TODO`、`TBD`、模糊的“后续实现”
- 一致性检查：
  - 使用统一名称：`OutboundEnvelope`、`send_envelope()`、`deliver_envelope()`、`default_delivery_policy()`

**Plan complete and saved to `docs/superpowers/plans/2026-09-20-router-unified-outbound-implementation.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - 我 dispatch 新 subagent 按任务执行**

**2. Inline Execution** - 我在当前会话按这份计划直接实现**

**Which approach?**
