# Unified Inbound Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为飞书消息、定时任务、子任务完成回流建立统一入站分发层，并同步对齐架构文档与注释。

**Architecture:** 新增 `UnifiedInboundEvent` 和 `InboundDispatcher`，让主要入站入口统一先进入 dispatcher，再映射为现有 `InboxEvent` 并送入 `InboxHub`。保留 `ResidentConsumer` 与 `kind` 分发逻辑不变，降低迁移风险。

**Tech Stack:** Python 3.x、`dataclasses`、现有 `InboxHub/InboxEvent/ResidentConsumer`、`pytest`

---

## 文件结构与职责

- Create: `src/agent_core/channel/inbound.py`
  - 统一入站事件模型与分发器
- Create: `tests/test_inbound_dispatcher.py`
  - 覆盖 dispatcher 标准化与提交流程
- Create: `docs/current-architecture-state.md`
  - 当前统一入站 + 统一出站实装架构文档
- Modify: `src/agent_core/channel/interfaces.py`
  - 修正文档注释
- Modify: `src/agent_core/channel/feishu/ws_client.py`
  - 飞书 SDK 回调改走 dispatcher
- Modify: `src/agent_core/runtime/subagent_pool.py`
  - 子任务完成改走 dispatcher
- Modify: `src/agent_core/scheduler.py`
  - 定时任务提交改走 dispatcher
- Modify: `docs/channels_and_router.md`
  - 增加实现状态说明

### Task 1: 新增 UnifiedInboundEvent 与 InboundDispatcher

**Files:**
- Create: `src/agent_core/channel/inbound.py`
- Test: `tests/test_inbound_dispatcher.py`

- [ ] **Step 1: 写失败测试**

```python
from agent_core.channel.inbound import InboundDispatcher
from agent_core.runtime.inbox import InboxHub


def test_dispatcher_submits_channel_message_to_hub():
    hub = InboxHub()
    dispatcher = InboundDispatcher(hub=hub)

    dispatcher.submit_channel_message(
        target="feishu:oc_x",
        payload={"event": 1},
        source="feishu",
        summary="group message",
    )

    batch = hub.drain("feishu:oc_x")
    assert len(batch) == 1
    assert batch[0].kind == "channel_message"
    assert batch[0].source == "feishu"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `pytest tests/test_inbound_dispatcher.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 写最小实现**

```python
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from agent_core.runtime.inbox import InboxEvent, InboxHub, get_hub


@dataclass
class UnifiedInboundEvent:
    target: str
    kind: str
    payload: Any = None
    source: str = ""
    summary: str = ""
    route_key: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class InboundDispatcher:
    def __init__(self, hub: Optional[InboxHub] = None) -> None:
        self.hub = hub or get_hub()

    def submit_unified(self, event: UnifiedInboundEvent) -> InboxEvent:
        inbox = InboxEvent(
            target=event.target,
            kind=event.kind,
            payload=event.payload,
            source=event.source,
            summary=event.summary,
        )
        return self.hub.submit(inbox)
```

- [ ] **Step 4: 增加三个显式 helper**

```python
def submit_channel_message(...): ...
def submit_timer(...): ...
def submit_subagent_done(...): ...
```

Run: `pytest tests/test_inbound_dispatcher.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/channel/inbound.py tests/test_inbound_dispatcher.py
git commit -m "feat: add unified inbound dispatcher"
```

### Task 2: 迁移飞书、调度器、子任务完成入口

**Files:**
- Modify: `src/agent_core/channel/feishu/ws_client.py`
- Modify: `src/agent_core/scheduler.py`
- Modify: `src/agent_core/runtime/subagent_pool.py`
- Test: `tests/test_inbound_dispatcher.py`
- Test: `tests/test_channel_p3.py`

- [ ] **Step 1: 写失败测试，锁定三类入口仍提交到原目标和原 kind**

```python
def test_dispatcher_submit_timer_keeps_kind_and_target():
    hub = InboxHub()
    dispatcher = InboundDispatcher(hub=hub)
    dispatcher.submit_timer(
        target="feishu:sched:daily",
        payload={"task": "x"},
        summary="定时任务 daily",
    )
    ev = hub.drain("feishu:sched:daily")[0]
    assert ev.kind == "timer"
```

- [ ] **Step 2: 改飞书入口**

```python
self.inbound.submit_channel_message(
    target=f"feishu:{chat_id}",
    payload=event_data,
    source="feishu",
    summary=f"{chat_type} {sender_open_id}",
)
```

- [ ] **Step 3: 改调度器与子任务完成**

```python
get_inbound_dispatcher().submit_timer(...)
get_inbound_dispatcher().submit_subagent_done(...)
```

Run: `pytest tests/test_inbound_dispatcher.py tests/test_channel_p3.py -q`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add src/agent_core/channel/feishu/ws_client.py src/agent_core/scheduler.py src/agent_core/runtime/subagent_pool.py tests/test_inbound_dispatcher.py
git commit -m "refactor: route inbound sources through dispatcher"
```

### Task 3: 对齐文档与注释

**Files:**
- Modify: `src/agent_core/channel/interfaces.py`
- Modify: `docs/channels_and_router.md`
- Create: `docs/current-architecture-state.md`

- [ ] **Step 1: 修正文档注释**

```python
"""
ChannelPort — 通道生命周期（编排器实现）
ChannelIO  — 渠道 I/O 原语（通道适配内部实现）

Agent 不直接依赖 ChannelIO。
统一出站走 Router；统一入站走 InboundDispatcher。
"""
```

- [ ] **Step 2: 更新架构文档状态**

Run: no command
Expected: `channels_and_router.md` 明确标注“部分落地：端点/目录/Router/ChannelManager/InboundDispatcher”

- [ ] **Step 3: 新增当前架构文档**

Run: no command
Expected: `docs/current-architecture-state.md` 描述统一入站 + 统一出站 + 文件职责映射

- [ ] **Step 4: 提交**

```bash
git add src/agent_core/channel/interfaces.py docs/channels_and_router.md docs/current-architecture-state.md
git commit -m "docs: align architecture docs with inbound and outbound design"
```

### Task 4: 综合回归

**Files:**
- Test: `tests/test_inbound_dispatcher.py`
- Test: `tests/test_channel_p1.py`
- Test: `tests/test_channel_p3.py`
- Test: `tests/test_channel_p4.py`
- Test: `tests/test_presenter_router.py`
- Test: `tests/test_router_outbound_envelope.py`

- [ ] **Step 1: 跑定向回归**

Run: `pytest tests/test_inbound_dispatcher.py tests/test_channel_p1.py tests/test_channel_p3.py tests/test_channel_p4.py tests/test_presenter_router.py tests/test_router_outbound_envelope.py -q`
Expected: PASS

- [ ] **Step 2: 如有兼容问题，做最小修正后再跑**

Run: `pytest tests/test_channel_p0.py tests/test_channel_p1.py tests/test_channel_p2.py tests/test_channel_p3.py tests/test_channel_p4.py tests/test_channel_p5.py tests/test_inbound_dispatcher.py tests/test_presenter_router.py tests/test_router_outbound_envelope.py tests/test_memory_pipeline.py tests/test_basic.py tests/test_prompt_layering.py -q`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add tests/test_inbound_dispatcher.py src/agent_core/channel/inbound.py src/agent_core/channel/feishu/ws_client.py src/agent_core/runtime/subagent_pool.py src/agent_core/scheduler.py
git commit -m "test: verify unified inbound dispatch regressions"
```

## 自检

- Spec 覆盖：统一入口、三类来源迁移、文档对齐、综合回归都已覆盖
- 无占位符
- 类型一致：`UnifiedInboundEvent` / `InboundDispatcher` / `submit_*`
