# NanoGhost Unified Inbound Dispatch Design

**Date:** 2026-09-20

**Status:** Proposed

**Goal**

把当前分散在 `ws_client`、`scheduler`、`subagent_pool` 的入站入口统一收口到正式的 `InboundDispatcher`，先标准化成统一入站事件，再统一映射到 `InboxEvent`，继续复用现有 `ResidentConsumer`。

## 背景

当前系统已经完成了统一出站：

- `TurnResponder` 不再直接执行 `ChannelIO`
- `OutboundEnvelope` 成为统一出站模型
- `Router` 成为正式出站入口

但入站仍是分散状态：

- 飞书 SDK 回调直接构造 `InboxEvent`
- 调度器直接调用 `submit_event(...)`
- 子任务池完成后直接调用 `submit_event(...)`

虽然它们最终都会进入 `InboxHub`，但目前没有一层正式的统一入站分发器。

## 设计结论

采用以下方案：

- 新增 `UnifiedInboundEvent`
- 新增 `InboundDispatcher`
- 所有主要入站来源先转成 `UnifiedInboundEvent`
- 再由 `InboundDispatcher` 统一映射成 `InboxEvent`
- 保留现有 `InboxEvent` / `ResidentConsumer` / `run_agent_turn()` 链路
- 不在本次重写 session target 规则

## 非目标

- 不改写 `ResidentConsumer` 的并发模型
- 不把 `InboxEvent` 替换掉
- 不在本次统一所有未来通道，只先把现有主要入口收口
- 不改动出站 `Router` 模型
- 不重写会话归并策略

## 目标架构

```text
Feishu SDK / Scheduler / SubAgentPool
  -> InboundDispatcher
  -> UnifiedInboundEvent
  -> InboxEvent
  -> InboxHub
  -> ResidentConsumer
  -> ws_client._handle_inbox_batch
  -> run_agent_turn / scheduler task handler / subagent completion handler
```

## 数据模型

### UnifiedInboundEvent

```python
@dataclass
class UnifiedInboundEvent:
    target: str
    kind: str
    source: str = ""
    summary: str = ""
    payload: Any = None
    route_key: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
```

说明：

- `target`：最终进入 `InboxHub` 的端点地址
- `kind`：统一事件类别，仍保持当前 `channel_message / timer / subagent_done`
- `source`：来源命名，如 `feishu` / `timer` / `subagent`
- `summary`：日志与观测摘要
- `payload`：原始内容
- `route_key`：为后续更复杂 target 规则预留，不在本次强用
- `meta`：来源附加信息

## InboundDispatcher 职责

`InboundDispatcher` 负责两件事：

1. 把不同来源标准化成 `UnifiedInboundEvent`
2. 把 `UnifiedInboundEvent` 映射为 `InboxEvent` 并提交到 `InboxHub`

建议接口：

- `submit_channel_message(target, payload, source, summary="", meta=None)`
- `submit_timer(target, payload, summary="", meta=None)`
- `submit_subagent_done(target, payload, summary="", meta=None)`
- `submit_unified(event)`

## 迁移范围

### 飞书入口

`FeishuWSClient._on_sdk_message()` 不再自己构造 `InboxEvent`，改为：

- 计算 target
- 调 `InboundDispatcher.submit_channel_message(...)`

### 调度器入口

`scheduler_loop()` 不再直接 `submit_event(...)`，改为：

- 调 `InboundDispatcher.submit_timer(...)`

### 子任务完成入口

`SubAgentPool._run_one()` 不再直接 `submit_event(...)`，改为：

- 调 `InboundDispatcher.submit_subagent_done(...)`

## 兼容策略

- `InboxEvent` 保持不变
- `ws_client._handle_inbox_batch()` 保持按 `kind` 分发
- 现有 `test_channel_p1/p3/p4` 仍应通过
- `submit_event()` 保留，不删除

## 文档对齐

本次同步更新：

- `src/agent_core/channel/interfaces.py` 中过时的 `ChannelIO` 注释
- `docs/channels_and_router.md` 的实现状态说明
- 新增一份当前实装架构文档，补“统一入站 + 统一出站”的真实主干

## 测试要求

至少覆盖：

1. `submit_channel_message()` 生成正确 `InboxEvent`
2. `submit_timer()` 生成正确 `InboxEvent`
3. `submit_subagent_done()` 生成正确 `InboxEvent`
4. `ws_client` 通过 dispatcher 提交
5. `scheduler` 通过 dispatcher 提交
6. `subagent_pool` 通过 dispatcher 提交
7. 现有 P1/P3/P4 测试不回归

## 风险

- 如果 target 计算被误改，事件会进错队列
- 如果 kind 被改乱，`_handle_inbox_batch()` 会分发错分支
- 如果 dispatcher 设计过厚，会把原本简单的入站提交流程弄复杂

## 设计摘要

- 入站统一到 `InboundDispatcher`
- 使用 `UnifiedInboundEvent -> InboxEvent` 两段式
- 继续复用现有 `InboxHub / ResidentConsumer`
- 先统一入口，不重写 session target 规则
