# NanoGhost Architecture Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不扩大功能面的前提下，把 `Router`、多通道抽象、记忆系统和观测面收拢成稳定的生产主干。

**Architecture:** 保持现有 `Agent` 执行内核、Tool/Skill/MCP 结构不动，优先补齐三条主链路：统一出站边界、统一通道治理、统一记忆后台流水线。Phase 1 只做兼容式收敛，不改外部协议；Phase 2 再补健康检查、观测与文档状态对齐。

**Tech Stack:** Python 3.x、`asyncio`、`pytest`、现有 `DatabasePort` / `LLMPort` 抽象、Feishu 适配层、现有内置工具体系。

---

## 现状基线

以下结论基于当前仓库真实代码，而不是只基于设计文档：

- `Router` 已经存在并可工作，核心实现位于 `src/agent_core/router.py`，具备 `resolve()`、`deliver()`、`send()`、`ACL`、`RateLimiter`、`Auditor`、`mirror`。
- `send_message` 内置工具已经通过 `Router` 发送消息，见 `src/agent_core/tool/builtins/send.py`。
- 飞书通道已经接入 `ChannelRegistry`、动态注册端点到 `EndpointDirectory`、并向 `Router` 注入 `mirror` 钩子，见 `src/agent_core/channel/feishu/ws_client.py`。
- 当前本轮回复仍主要通过 `ChannelIO` 在 `src/agent_core/presenter.py` 里直接下发，`Router` 还不是统一出站主干。
- 记忆召回已在 `src/agent_core/engine/messages.py` 中接入 `retrieve_similar_flows()`。
- 记忆写入已在 `src/agent_core/memory/postprocess.py` 中串行执行：`summarize_intent()` → `record_successful_flow()` → `update_graph_ml()` → `memory.md`。
- `docs/channels_and_router.md` 描述了更完整的目标形态，但其文档头部已明确标注“设计稿，未动代码”；实施时必须以 `src/` 中真实落地结构为准。

## 优化项总览

### O1. 统一出站边界

- 目标：明确“当前会话回复”和“跨端点主动发送”是两条不同路径。
- 现状问题：`Presenter` 与 `Router` 边界不清，未来加多通道时会出现护栏绕过、镜像不一致、审计口径不一致。
- 结果定义：
  - 当前来源会话的 reply 继续走 `ChannelIO`
  - 跨端点 / 主动通知 / fan-out 全部只走 `Router`
  - `Router` 成为主动出站唯一入口

### O2. 多通道治理控制面

- 目标：在 `ChannelRegistry + EndpointDirectory` 之上增加统一管理层，不再把健康、启停、能力判断分散在各处。
- 现状问题：通道注册和端点目录已经有了，但还缺启停、状态、可发性判断与稳定的只读视图。
- 结果定义：
  - 增加 `ChannelManager`
  - `Router.deliver()` 能检查端点启停、状态、能力、通道可用性
  - 管理接口统一从 `ChannelManager` 暴露

### O3. Presenter 收敛

- 目标：把 `Presenter` 从“既编排又发送又渲染”的大函数拆成清晰的输出边界。
- 现状问题：`src/agent_core/presenter.py` 同时负责 session 拼装、reaction、工具反馈、最终发送、图片输出。
- 结果定义：
  - 新增轻量 `TurnResponder`
  - `Presenter` 专注事件流消费和文本决策
  - `TurnResponder` 负责 `reply_current()`、`send_current()`、`send_images()`

### O4. 记忆后台流水线

- 目标：把回合后记忆写入从“串行后处理”升级为“可观测、可异步、可降级”的后台流程。
- 现状问题：记忆写入阶段过多，虽然在异步线程中执行，但缺统一队列、失败统计、关闭开关和状态可见性。
- 结果定义：
  - 引入 `MemoryPipeline`
  - `Agent` 在回合成功后只负责提交 `MemoryTurnEvent`
  - 流水线后台执行 `intent/card/graph/memory.md`

### O5. 记忆召回稳态化

- 目标：降低“语义相似但链路不对”的误召回概率。
- 现状问题：`retrieve_similar_flows()` 主要依赖 embedding + cosine + MMR，排序信号不足。
- 结果定义：
  - 增加 `updated_at`、`acceptance_score`、`l1_code` 等稳定排序信号
  - 支持可选的 hint-based hard filter
  - 为后续“规则前筛 + 语义后排”预留参数接口

### O6. 观测与文档状态对齐

- 目标：让路由、通道、记忆三条链路都有可读状态与文档实现状态。
- 现状问题：观测指标稀缺；文档设计状态与当前实现状态没有统一标注。
- 结果定义：
  - 新增 memory / router / channel 统计输出
  - 更新 `docs/channels_and_router.md` 的实现状态
  - 为后续诊断命令或管理页面提供基础数据

## 文件结构与职责锁定

### 新建文件

- `src/agent_core/channel/manager.py`
  - 统一管理通道启停、端点启停、能力判断、健康状态、只读视图
- `src/agent_core/channel/responder.py`
  - 当前会话输出边界，封装 reply/send/image 行为
- `src/agent_core/memory/pipeline.py`
  - 记忆后台事件队列、工作协程、统计数据
- `tests/test_channel_p5.py`
  - 覆盖 `ChannelManager`、Router 新护栏、管理接口
- `tests/test_presenter_router.py`
  - 覆盖 `Presenter` 与 `TurnResponder` 的边界
- `tests/test_memory_pipeline.py`
  - 覆盖记忆事件提交、排队、失败不影响主链
- `tests/test_memory_retrieval.py`
  - 覆盖新排序与 hint filter

### 修改文件

- `src/agent_core/router.py`
  - 注入 `ChannelManager`，补 endpoint/status/capability 守卫
- `src/agent_core/channel/__init__.py`
  - 导出 `ChannelManager`
- `src/agent_core/channel/admin.py`
  - 改为通过 `ChannelManager` 聚合视图
- `src/agent_core/channel/feishu/ws_client.py`
  - 启动时注册通道管理器，继续保留 endpoint 动态注册与 mirror
- `src/agent_core/presenter.py`
  - 接入 `TurnResponder`，把 I/O 细节从主流程剥离
- `src/agent_core/engine/agent.py`
  - 从直接调用 `postprocess_turn()` 改为提交 `MemoryPipeline`
- `src/agent_core/memory/postprocess.py`
  - 保持阶段处理逻辑，但改为由 pipeline 调度
- `src/agent_core/memory/cards.py`
  - 增加排序 / hint 过滤 / 统计字段写入
- `src/agent_core/engine/messages.py`
  - 透传检索 hint 与新排序上下文
- `docs/channels_and_router.md`
  - 标注“已落地 / 部分落地 / 仅设计”

---

### Task 1: 建立 ChannelManager 并让 Router 识别端点状态

**Files:**
- Create: `src/agent_core/channel/manager.py`
- Modify: `src/agent_core/channel/__init__.py`
- Modify: `src/agent_core/router.py`
- Modify: `src/agent_core/channel/admin.py`
- Test: `tests/test_channel_p5.py`

- [ ] **Step 1: 写失败测试，锁定端点启停和能力守卫**

```python
from agent_core.channel.base import Channel
from agent_core.channel.manager import ChannelManager
from agent_core.router import Router


class FakeChannel(Channel):
    name = "fake"

    def __init__(self):
        self.calls = []

    def send(self, target: str, text: str) -> bool:
        self.calls.append((target, text))
        return True

    def reply(self, message_id: str, text: str) -> bool:
        return True

    def capabilities(self):
        return {"text"}


def test_router_skips_disabled_endpoint():
    mgr = ChannelManager()
    mgr.register_channel(FakeChannel())
    mgr.register_endpoint("fake:a", channel="fake", kind="group", capabilities={"text"})
    mgr.enable_endpoint("fake:a", False)

    rep = Router(registry=mgr.registry, manager=mgr).deliver(["fake:a"], "hi")

    assert rep["sent"] == []
    assert rep["skipped"] == ["fake:a"]
    assert rep["reasons"]["fake:a"] == "endpoint_disabled"


def test_router_skips_endpoint_without_text_capability():
    mgr = ChannelManager()
    mgr.register_channel(FakeChannel())
    mgr.register_endpoint("fake:b", channel="fake", kind="group", capabilities={"image"})

    rep = Router(registry=mgr.registry, manager=mgr).deliver(["fake:b"], "hi")

    assert rep["sent"] == []
    assert rep["skipped"] == ["fake:b"]
    assert rep["reasons"]["fake:b"] == "missing_capability:text"
```

- [ ] **Step 2: 运行测试，确认当前代码缺少控制面**

Run: `pytest tests/test_channel_p5.py -q`
Expected: FAIL，提示 `ModuleNotFoundError: No module named 'agent_core.channel.manager'` 或 `Router.__init__() got an unexpected keyword argument 'manager'`

- [ ] **Step 3: 写最小实现，增加统一控制面**

```python
# src/agent_core/channel/manager.py
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from .directory import EndpointDirectory, get_directory
from .endpoint import Endpoint
from .registry import ChannelRegistry, get_registry


class ChannelManager:
    def __init__(self, registry: Optional[ChannelRegistry] = None, directory: Optional[EndpointDirectory] = None) -> None:
        self.registry = registry or get_registry()
        self.directory = directory or get_directory()
        self._channel_enabled: Dict[str, bool] = {}
        self._endpoint_enabled: Dict[str, bool] = {}
        self._health: Dict[str, Dict[str, Any]] = {}

    def register_channel(self, channel):
        return self.registry.register(channel)

    def register_endpoint(self, addr: str, **kw) -> Endpoint:
        return self.directory.register(addr, **kw)

    def get_endpoint(self, addr: str) -> Optional[Endpoint]:
        return self.directory.get(addr)

    def enable_channel(self, name: str, enabled: bool) -> None:
        self._channel_enabled[name] = bool(enabled)

    def enable_endpoint(self, addr: str, enabled: bool) -> None:
        self._endpoint_enabled[addr] = bool(enabled)

    def is_channel_enabled(self, name: str) -> bool:
        return self._channel_enabled.get(name, True)

    def is_endpoint_enabled(self, addr: str) -> bool:
        return self._endpoint_enabled.get(addr, True)

    def can_send_text(self, addr: str) -> tuple[bool, str]:
        ep = self.get_endpoint(addr)
        if ep is None:
            return True, ""
        if not self.is_endpoint_enabled(addr):
            return False, "endpoint_disabled"
        if not self.is_channel_enabled(ep.channel):
            return False, "channel_disabled"
        if ep.status not in ("online", ""):
            return False, f"endpoint_status:{ep.status}"
        if ep.capabilities and "text" not in ep.capabilities:
            return False, "missing_capability:text"
        return True, ""

    def set_health(self, key: str, status: Dict[str, Any]) -> None:
        self._health[key] = dict(status)

    def health(self, key: str) -> Dict[str, Any]:
        return dict(self._health.get(key, {}))
```

```python
# src/agent_core/router.py
class Router:
    def __init__(self, registry=None, *, manager=None, mirror=None) -> None:
        self.registry = registry
        self.manager = manager
        self.mirror = mirror
        self.acl = Acl()
        self.rate = RateLimiter()
        self.auditor = Auditor()

    def _manager(self):
        if self.manager is not None:
            return self.manager
        from agent_core.channel.manager import ChannelManager
        return ChannelManager(registry=self._registry())

    def deliver(self, targets, text, *, source_addr="", agent_key="default", hop=0):
        ...
        for addr in targets:
            channel, target = parse_addr(addr) if ":" in addr else ("", addr)
            allowed, reason = self._manager().can_send_text(addr)
            if not allowed:
                skipped.append(addr)
                reasons[addr] = reason
                self._audit(addr, text, f"denied:{reason}", agent_key)
                continue
            ...
```

- [ ] **Step 4: 跑测试并补管理只读视图**

```python
# src/agent_core/channel/admin.py
from .manager import ChannelManager


def channel_overview(manager: ChannelManager | None = None):
    mgr = manager or ChannelManager()
    out = []
    for name in mgr.registry.names():
        eps = mgr.directory.list(channel=name)
        ch = mgr.registry.get(name)
        out.append({
            "channel": name,
            "enabled": mgr.is_channel_enabled(name),
            "capabilities": sorted(ch.capabilities()) if ch else [],
            "endpoints": len(eps),
            "online": len([e for e in eps if e.status == "online"]),
        })
    return out
```

Run: `pytest tests/test_channel_p5.py tests/test_channel_p2.py -q`
Expected: PASS，且旧的 `P2` 路由测试不回归

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/channel/manager.py src/agent_core/channel/__init__.py src/agent_core/router.py src/agent_core/channel/admin.py tests/test_channel_p5.py
git commit -m "feat: add channel manager and router transport guards"
```

### Task 2: 把 Presenter 的 I/O 细节收敛到 TurnResponder

**Files:**
- Create: `src/agent_core/channel/responder.py`
- Modify: `src/agent_core/presenter.py`
- Test: `tests/test_presenter_router.py`

- [ ] **Step 1: 写失败测试，锁定 reply 与主动发送边界**

```python
from agent_core.channel.responder import TurnResponder


class FakeIO:
    def __init__(self):
        self.sent = []
        self.replied = []
        self.images = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True

    def reply(self, message_id, text):
        self.replied.append((message_id, text))
        return True

    def send_images(self, chat_id, images):
        self.images.append((chat_id, list(images)))
        return {"ok": True}


class FakeRouter:
    def __init__(self):
        self.calls = []

    def send(self, to, text, **kw):
        self.calls.append((to, text, kw))
        return {"ok": True, "sent": to or []}


def test_turn_responder_replies_current_message_without_router():
    io = FakeIO()
    router = FakeRouter()
    responder = TurnResponder(io=io, router=router, platform="feishu", chat_id="oc_x", message_id="om_1")

    responder.reply_current("done")

    assert io.replied == [("om_1", "done")]
    assert router.calls == []


def test_turn_responder_sends_proactive_message_via_router():
    io = FakeIO()
    router = FakeRouter()
    responder = TurnResponder(io=io, router=router, platform="feishu", chat_id="oc_x", message_id="om_1")

    responder.notify(["feishu:ou_a"], "hello")

    assert router.calls[0][0] == ["feishu:ou_a"]
    assert io.sent == []
```

- [ ] **Step 2: 运行测试，确认当前没有统一输出边界**

Run: `pytest tests/test_presenter_router.py -q`
Expected: FAIL，提示 `ModuleNotFoundError: No module named 'agent_core.channel.responder'`

- [ ] **Step 3: 写最小实现，抽出 TurnResponder**

```python
# src/agent_core/channel/responder.py
from __future__ import annotations

from typing import Iterable, Optional


class TurnResponder:
    def __init__(self, *, io, router, platform: str, chat_id: str, message_id: str = "") -> None:
        self.io = io
        self.router = router
        self.platform = platform
        self.chat_id = chat_id
        self.message_id = message_id

    def reply_current(self, text: str) -> bool:
        if self.message_id:
            return bool(self.io.reply(self.message_id, text))
        return bool(self.io.send_text(self.chat_id, text))

    def send_current(self, text: str) -> bool:
        return bool(self.io.send_text(self.chat_id, text))

    def send_images(self, images: Iterable[str]) -> None:
        imgs = list(images)
        if imgs:
            self.io.send_images(self.chat_id, imgs[:10])

    def notify(self, to, text: str, *, namespace: str = "default"):
        return self.router.send(
            to,
            text,
            ctx={"source_addr": f"{self.platform}:{self.chat_id}"},
            agent_key=namespace,
        )
```

- [ ] **Step 4: 在 Presenter 中替换直接 I/O 调用**

```python
# src/agent_core/presenter.py
from agent_core.channel.responder import TurnResponder
from agent_core.router import get_router


responder = TurnResponder(
    io=io,
    router=get_router(),
    platform=source.platform,
    chat_id=chat_id,
    message_id=message_id,
)

...
if ev_type == "tool_call" and feedback_level >= 3:
    responder.send_current(msg)

...
if ev_type == "done":
    if reply_text:
        responder.reply_current(reply_text)

...
if out_images:
    responder.send_images(out_images)
```

Run: `pytest tests/test_presenter_router.py tests/test_channel_p2.py tests/test_reaction.py -q`
Expected: PASS，当前会话 reply 保持原行为，主动发送仍经 `send_message` / `Router`

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/channel/responder.py src/agent_core/presenter.py tests/test_presenter_router.py
git commit -m "refactor: isolate presenter io through turn responder"
```

### Task 3: 引入 MemoryPipeline，把 postprocess 变成后台事件流

**Files:**
- Create: `src/agent_core/memory/pipeline.py`
- Modify: `src/agent_core/engine/agent.py`
- Modify: `src/agent_core/memory/postprocess.py`
- Test: `tests/test_memory_pipeline.py`

- [ ] **Step 1: 写失败测试，锁定“提交即返回、后台处理”的行为**

```python
import asyncio

from agent_core.memory.pipeline import MemoryPipeline, MemoryTurnEvent


async def test_memory_pipeline_processes_event():
    seen = []

    async def handler(event: MemoryTurnEvent):
        seen.append(event.session_id)

    pipe = MemoryPipeline(handler=handler)
    await pipe.start()
    try:
        await pipe.submit(MemoryTurnEvent(session_id="sess-1", user_message="u", reply="r", all_steps_out=[], step_count=1))
        await asyncio.sleep(0.05)
    finally:
        await pipe.stop()

    assert seen == ["sess-1"]
```

- [ ] **Step 2: 运行测试，确认当前没有后台记忆流水线**

Run: `pytest tests/test_memory_pipeline.py -q`
Expected: FAIL，提示 `ModuleNotFoundError: No module named 'agent_core.memory.pipeline'`

- [ ] **Step 3: 写最小实现，建立队列和统计**

```python
# src/agent_core/memory/pipeline.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional


@dataclass
class MemoryTurnEvent:
    session_id: str | None
    user_message: str
    reply: str
    all_steps_out: list
    step_count: int


class MemoryPipeline:
    def __init__(self, handler: Callable[[MemoryTurnEvent], Awaitable[None]]) -> None:
        self._handler = handler
        self._q: asyncio.Queue[MemoryTurnEvent] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._stats = {"submitted": 0, "processed": 0, "failed": 0}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            await asyncio.wait([self._task], timeout=1)

    async def submit(self, event: MemoryTurnEvent) -> None:
        self._stats["submitted"] += 1
        await self._q.put(event)

    def stats(self) -> dict:
        return dict(self._stats)

    async def _run(self) -> None:
        while self._running:
            event = await self._q.get()
            try:
                await self._handler(event)
                self._stats["processed"] += 1
            except Exception:
                self._stats["failed"] += 1
```

- [ ] **Step 4: 在 Agent 中改为提交事件，不直接调 postprocess**

```python
# src/agent_core/engine/agent.py
from agent_core.memory.pipeline import MemoryPipeline, MemoryTurnEvent
from agent_core.memory.postprocess import postprocess_turn


async def _handle_memory_turn(self, event: MemoryTurnEvent) -> None:
    await postprocess_turn(
        agent=self,
        user_message=event.user_message,
        reply=event.reply,
        all_steps_out=event.all_steps_out,
        step_counter=[event.step_count],
        session_id=event.session_id,
    )


if not hasattr(self, "_memory_pipeline"):
    self._memory_pipeline = MemoryPipeline(handler=self._handle_memory_turn)
    await self._memory_pipeline.start()

await self._memory_pipeline.submit(
    MemoryTurnEvent(
        session_id=session_id,
        user_message=user_message,
        reply=reply_text,
        all_steps_out=all_steps_out,
        step_count=step_counter[0],
    )
)
```

Run: `pytest tests/test_memory_pipeline.py tests/test_basic.py tests/test_prompt_layering.py -q`
Expected: PASS，主链不等待所有记忆阶段完成

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/memory/pipeline.py src/agent_core/engine/agent.py src/agent_core/memory/postprocess.py tests/test_memory_pipeline.py
git commit -m "feat: add async memory pipeline for turn postprocess"
```

### Task 4: 稳定化记忆召回排序，并预留 hint filter

**Files:**
- Modify: `src/agent_core/memory/cards.py`
- Modify: `src/agent_core/engine/messages.py`
- Test: `tests/test_memory_retrieval.py`

- [ ] **Step 1: 写失败测试，锁定排序与 hint 行为**

```python
from agent_core.memory.cards import retrieve_similar_flows


class FakeDB:
    def load_all_memory_cards(self, namespace=None):
        return [
            {
                "flow_hash": "a",
                "intent_summary": "创建工单",
                "intent_vector": [1.0, 0.0],
                "success_count": 10,
                "approved_count": 3,
                "rejected_count": 0,
                "updated_at": 200.0,
                "l1_code": 100,
            },
            {
                "flow_hash": "b",
                "intent_summary": "创建工单后通知",
                "intent_vector": [1.0, 0.0],
                "success_count": 10,
                "approved_count": 0,
                "rejected_count": 3,
                "updated_at": 300.0,
                "l1_code": 200,
            },
        ]


class FakeLLM:
    pass


def test_retrieve_similar_flows_prefers_acceptance_over_raw_recency(monkeypatch):
    monkeypatch.setattr("agent_core.memory.cards.get_embedding", lambda text, llm=None: [1.0, 0.0])
    monkeypatch.setattr("agent_core.memory.cards.cosine", lambda a, b: 0.9)
    monkeypatch.setattr("agent_core.memory.cards.mmr_select", lambda items, q_vec, top_k, lambda_mult=0.7: items[:top_k])

    picked = retrieve_similar_flows("创建工单", db=FakeDB(), llm=FakeLLM(), namespace="ns", top_k=2)

    assert picked[0]["flow_hash"] == "a"


def test_retrieve_similar_flows_applies_l1_hint(monkeypatch):
    monkeypatch.setattr("agent_core.memory.cards.get_embedding", lambda text, llm=None: [1.0, 0.0])
    monkeypatch.setattr("agent_core.memory.cards.cosine", lambda a, b: 0.9)
    monkeypatch.setattr("agent_core.memory.cards.mmr_select", lambda items, q_vec, top_k, lambda_mult=0.7: items[:top_k])

    picked = retrieve_similar_flows("创建工单", db=FakeDB(), llm=FakeLLM(), namespace="ns", top_k=2, l1_hint=100)

    assert [m["flow_hash"] for m in picked] == ["a"]
```

- [ ] **Step 2: 运行测试，确认当前检索缺少 hint 和排序权重**

Run: `pytest tests/test_memory_retrieval.py -q`
Expected: FAIL，提示 `retrieve_similar_flows() got an unexpected keyword argument 'l1_hint'`

- [ ] **Step 3: 写最小实现，补排序信号与 hint 过滤**

```python
# src/agent_core/memory/cards.py
def retrieve_similar_flows(
    user_intent: str,
    top_k: int = 3,
    scene_tag: Optional[str] = None,
    *,
    increment_trigger: bool = False,
    db: Optional[DatabasePort] = None,
    llm: Optional[LLMPort] = None,
    namespace: Optional[str] = None,
    l1_hint: Optional[int] = None,
) -> List[Dict[str, Any]]:
    ...
    for it in items:
        if l1_hint is not None and int(it.get("l1_code") or 0) != int(l1_hint):
            continue
        ...
        total_fb = approved + rejected
        acceptance = approved / float(total_fb) if total_fb > 0 else float(succ)
        recency = float(it.get("updated_at") or 0.0)
        mm["_acceptance_score"] = acceptance
        mm["_recency_score"] = recency
        raw_candidates.append(mm)

    def _sort_key(m: Dict[str, Any]) -> tuple[float, float, float]:
        return (
            float(m.get("_acceptance_score") or 0.0),
            float(m.get("_similarity") or 0.0),
            float(m.get("_recency_score") or 0.0),
        )
```

- [ ] **Step 4: 在 messages 构建时透传可选 hint**

```python
# src/agent_core/engine/messages.py
similar_flows = retrieve_similar_flows(
    user_message,
    top_k=2,
    increment_trigger=False,
    db=db,
    llm=llm,
    namespace=namespace,
    l1_hint=(channel_ctx or {}).get("memory_l1_hint") if isinstance(channel_ctx, dict) else None,
) or []
```

Run: `pytest tests/test_memory_retrieval.py tests/test_memory_files.py -q`
Expected: PASS，且旧的 memory 文件测试不回归

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/memory/cards.py src/agent_core/engine/messages.py tests/test_memory_retrieval.py
git commit -m "feat: stabilize memory retrieval ranking and hint filters"
```

### Task 5: 为 channel / memory / router 增加状态视图与统计

**Files:**
- Modify: `src/agent_core/channel/admin.py`
- Modify: `src/agent_core/memory/pipeline.py`
- Modify: `src/agent_core/router.py`
- Test: `tests/test_channel_p5.py`
- Test: `tests/test_memory_pipeline.py`

- [ ] **Step 1: 写失败测试，锁定状态输出**

```python
from agent_core.channel.admin import channel_metrics, channel_overview
from agent_core.memory.pipeline import MemoryPipeline


def test_channel_overview_includes_enabled_flag():
    from agent_core.channel.manager import ChannelManager
    from agent_core.channel.base import Channel

    class FakeChannel(Channel):
        name = "fake"
        def send(self, target, text): return True
        def reply(self, message_id, text): return True

    mgr = ChannelManager()
    mgr.register_channel(FakeChannel())
    mgr.enable_channel("fake", False)

    rows = channel_overview(manager=mgr)
    assert rows[0]["channel"] == "fake"
    assert rows[0]["enabled"] is False


def test_memory_pipeline_exposes_stats(asyncio_run):
    async def handler(event):
        return None

    async def run():
        pipe = MemoryPipeline(handler=handler)
        await pipe.start()
        await pipe.submit(type("Evt", (), {"session_id": "s", "user_message": "u", "reply": "r", "all_steps_out": [], "step_count": 1})())
        await pipe.stop()
        stats = pipe.stats()
        assert "submitted" in stats

    asyncio_run(run())
```

- [ ] **Step 2: 运行测试，确认当前状态输出不完整**

Run: `pytest tests/test_channel_p5.py tests/test_memory_pipeline.py -q`
Expected: FAIL，缺少 `enabled`、pipeline 统计或相关 helper

- [ ] **Step 3: 写最小实现，统一只读统计口径**

```python
# src/agent_core/channel/admin.py
def channel_metrics(limit: int = 1000) -> Dict[str, int]:
    from agent_core.router import get_router
    recs = get_router().auditor.recent(limit)
    sent = sum(1 for r in recs if r.get("action") == "sent")
    denied = sum(1 for r in recs if str(r.get("action", "")).startswith("denied"))
    failed = sum(1 for r in recs if str(r.get("action", "")).startswith("failed"))
    return {"sent": sent, "denied": denied, "failed": failed, "records": len(recs)}
```

```python
# src/agent_core/memory/pipeline.py
def stats(self) -> dict:
    return {
        "submitted": self._stats["submitted"],
        "processed": self._stats["processed"],
        "failed": self._stats["failed"],
        "queue_size": self._q.qsize(),
        "running": self._running,
    }
```

- [ ] **Step 4: 让 Feishu 启动时接入统一管理器**

```python
# src/agent_core/channel/feishu/ws_client.py
from agent_core.channel.manager import ChannelManager

...
self.channel_manager = ChannelManager()
self.channel_manager.register_channel(self.channel)
...
self.channel_manager.register_endpoint(
    f"feishu:{source.chat_id}",
    channel="feishu",
    kind=kind,
    capabilities=self.channel.capabilities(),
    meta={...},
)
```

Run: `pytest tests/test_channel_p0.py tests/test_channel_p2.py tests/test_channel_p5.py tests/test_memory_pipeline.py -q`
Expected: PASS，且 Feishu 端点注册继续可用

- [ ] **Step 5: 提交**

```bash
git add src/agent_core/channel/admin.py src/agent_core/memory/pipeline.py src/agent_core/channel/feishu/ws_client.py tests/test_channel_p5.py tests/test_memory_pipeline.py
git commit -m "feat: add transport and memory runtime stats"
```

### Task 6: 更新架构文档状态并补实施说明

**Files:**
- Modify: `docs/channels_and_router.md`
- Modify: `README.md`
- Test: `tests/test_prompt_layering.py`

- [ ] **Step 1: 补文档状态区，避免把设计稿误读为现状**

```md
## 实现状态

- 已落地：
  - `ChannelRegistry`
  - `EndpointDirectory`
  - `Router` 基础投递 / ACL / rate / mirror
  - Feishu 通道注册与动态端点登记
- 部分落地：
  - 通道管理只读视图
  - 出站镜像
  - 每端点消费者
- 仅设计：
  - 完整 alias/group 策略
  - 更丰富的健康检查和观测面板
```

- [ ] **Step 2: 在 README 追加“架构状态”小节**

```md
## Architecture Status

- Router: 已接入 `send_message` 工具与 Feishu mirror，仍在推进统一主动出站边界
- Channel: `registry + directory` 已可用，`manager` 负责统一启停与能力判断
- Memory: `cards + graph + memory.md` 已可用，`pipeline` 负责后台沉淀
```

- [ ] **Step 3: 运行回归测试，确认文档修改未影响提示词或打包文案**

Run: `pytest tests/test_prompt_layering.py tests/test_basic.py -q`
Expected: PASS

- [ ] **Step 4: 记录发布说明**

```md
## Rollout Notes

- 先部署兼容版本，不删除任何旧接口
- 发布后观察 3 类指标：router denied、memory failed、channel endpoint offline
- 若 memory pipeline 异常增高，允许临时关闭后台记忆写入并保留主对话
```

- [ ] **Step 5: 提交**

```bash
git add docs/channels_and_router.md README.md
git commit -m "docs: align architecture docs with current implementation status"
```

---

## 测试矩阵

- 路由与通道：
  - `pytest tests/test_channel_p0.py tests/test_channel_p2.py tests/test_channel_p5.py -q`
- Presenter 与输出边界：
  - `pytest tests/test_presenter_router.py tests/test_reaction.py -q`
- 记忆流水线与召回：
  - `pytest tests/test_memory_pipeline.py tests/test_memory_retrieval.py tests/test_memory_files.py -q`
- 总体回归：
  - `pytest tests/test_basic.py tests/test_agent_mode.py tests/test_prompt_layering.py -q`

## 风险与回滚

- Router 护栏变严后，可能暴露“以前能发、现在因 endpoint 状态或能力被拒绝”的兼容问题。
- Presenter 拆层后，若 reply/send 行为变更不当，最容易影响飞书首轮回复与图片回发。
- MemoryPipeline 若队列阻塞，不能影响主对话；必须允许关闭或降级。
- 回滚顺序：
  - 先回滚 `engine/agent.py` 中的 pipeline 提交逻辑
  - 再回滚 `presenter.py` 中的 `TurnResponder`
  - 最后回滚 `router.py` 新增的 manager 守卫

## 自检

- 覆盖性检查：
  - O1 对应 Task 2
  - O2 对应 Task 1 / Task 5
  - O3 对应 Task 2
  - O4 对应 Task 3
  - O5 对应 Task 4
  - O6 对应 Task 5 / Task 6
- 占位符检查：
  - 全文未使用 `TODO` / `TBD` / “后续补充” 之类占位表达
- 一致性检查：
  - `ChannelManager`、`TurnResponder`、`MemoryPipeline`、`MemoryTurnEvent` 的命名在全部任务中保持一致

**Plan complete and saved to `docs/superpowers/plans/2026-09-20-nanoghost-architecture-optimization.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - 我按任务逐个起新 subagent 执行，并在任务之间给你审阅点**

**2. Inline Execution** - 我在当前会话里按这份计划直接推进实现**

**Which approach?**
