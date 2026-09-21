# NanoGhost Single Router Unified Route Envelope Design

**Date:** 2026-09-21

**Status:** Proposed

**Goal**

把当前“统一入站 `InboundDispatcher` + 统一出站 `Router`”的双入口架构，收口为“单 Router + 单消息模型”的最终形态。所有入站、出站、定时任务、子任务完成回流都统一走 `Router.submit(...)`，由 `Router` 在内部根据方向语义分流到入站队列或出站通道。

## 背景

当前仓库已经完成两件关键工作：

- 出站统一：`OutboundEnvelope -> Router -> Channel -> ChannelIO`
- 入站统一：`InboundDispatcher -> InboxEvent -> InboxHub -> ResidentConsumer`

这套分步改造在工程上是合理的，但从最终架构视角看，仍然存在两个并列的一等入口：

- `InboundDispatcher`：负责入站标准化与投递
- `Router`：负责出站解析与投递

这意味着当前主干仍然可以被理解为“两套路由系统”：

- 一套负责进入系统
- 一套负责离开系统

这与项目当前追求的目标不完全一致。项目的长期目标应当是：

- 所有消息都走一个总路由层
- 所有消息都使用一个统一语义模型
- `InboxHub` 和 `Channel` 都退化为 `Router` 的下游
- `Agent`、`Presenter`、通道入口都不再面对多个并列入口对象

## 设计结论

采用以下方案：

- 只保留一个一等入口：`Router`
- 新增统一消息模型：`RouteEnvelope`
- 入站和出站都使用 `RouteEnvelope`
- `Router.submit(envelope)` 作为唯一公开入口
- `Router` 内部保留 `inbound` / `outbound` 两种方向语义
- `InboxEvent` 保留，但降级为 `Router` 入站下游内部对象
- `Channel` / `ChannelIO` 保留，但继续作为 `Router` 出站下游对象
- 兼容迁移现有 `UnifiedInboundEvent` / `OutboundEnvelope`，再逐步清理

## 非目标

本次设计不做以下事项：

- 不重写 `ResidentConsumer` 的并发模型
- 不重写 `InboxHub` 的队列实现
- 不重写 `Channel` / `ChannelIO` 的平台能力接口
- 不删除飞书现有 `reply / images / reaction` 能力
- 不在本次重写 session target 归并规则
- 不同步重做 `Watcher`、`Sink` 等外围体系

## 目标架构

### 总体主干

```text
外部来源 / 编排输出 / 调度器 / 子任务完成
  -> RouteEnvelope
  -> Router.submit(...)
      -> normalize(...)
      -> route_inbound(...)  -> adapt_to_inbox(...)   -> InboxHub
      -> route_outbound(...) -> deliver_outbound(...) -> Channel -> ChannelIO
```

### 对外视角

对外只保留一句话：

```text
所有消息进出都走 Router
```

对调用方而言：

- 飞书 WS 回调不再面对 `InboundDispatcher`
- 调度器不再面对 `InboundDispatcher`
- 子任务完成回流不再面对 `InboundDispatcher`
- `Presenter / TurnResponder` 不再面对 `OutboundEnvelope` 专有入口

统一改为：

- 构造 `RouteEnvelope`
- 调 `Router.submit(...)`

### 对内视角

虽然入口对象收口为一个，但 `Router` 内部仍保留两种方向语义：

- `direction="inbound"`
- `direction="outbound"`

也就是说：

- 统一的是入口对象和消息模型
- 不是强行把入站、出站的处理步骤做成完全一样

## 数据模型

### RouteEnvelope

建议引入统一消息模型：

```python
@dataclass
class RouteEnvelope:
    direction: str                    # inbound | outbound
    kind: str                         # channel_message | timer | subagent_done | text | image | reaction | ...

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

### 字段语义

- `direction`
  - 标识当前是入站还是出站
  - 由 `Router` 内部据此决定路由分支

- `kind`
  - 标识当前语义类别
  - 入站阶段典型值：`channel_message` / `timer` / `subagent_done`
  - 出站阶段典型值：`text` / `image` / `reaction`

- `source_addr`
  - 源端点地址
  - 对入站表示消息进入系统前的来源地址
  - 对出站表示当前会话或源会话地址

- `target_addr`
  - 单目标地址
  - 主要用于入站投递到 `InboxHub` 前的单一目标表达

- `to`
  - 多目标地址列表
  - 主要用于出站 fan-out

- `payload`
  - 任意原始内容
  - 承接飞书原始事件、定时任务对象、子任务完成结果等

- `text`
  - 出站文本内容

- `images`
  - 出站图片数据列表

- `reply_to`
  - 目标消息回复语义
  - 延续当前 `reply_to` 能力，不因模型统一而删除

- `reaction`
  - 反应/表态相关扩展字段

- `agent_key`
  - 出站 ACL 与治理时使用

- `summary`
  - 日志摘要、审计摘要

- `route_key`
  - 为未来更复杂的 target 规则和 session 归并策略预留

- `meta`
  - 平台附加字段与策略扩展位

## Router 职责

### 唯一正式入口

`Router` 成为唯一一等入口对象。

建议对外公开：

- `submit(envelope: RouteEnvelope) -> dict | InboxEvent`

现有其它入口都应被降级为兼容包装：

- `send_envelope(...)`
- `deliver_envelope(...)`
- `InboundDispatcher.submit_*()`

### 内部接口建议

建议 `Router` 内部按以下阶段组织：

- `submit(envelope)`
- `normalize(envelope)`
- `route_inbound(envelope)`
- `route_outbound(envelope)`
- `adapt_to_inbox(envelope)`
- `deliver_outbound(envelope)`

其中：

- `submit()`：唯一公开入口
- `normalize()`：补默认值、校验、做统一模型约束
- `route_inbound()`：处理入站信封
- `route_outbound()`：处理出站信封
- `adapt_to_inbox()`：把 `RouteEnvelope(direction='inbound')` 降为 `InboxEvent`
- `deliver_outbound()`：把 `RouteEnvelope(direction='outbound')` 投递给 `Channel`

## 入站语义

### 入站来源

本次统一后，以下来源都应直接构造 `RouteEnvelope(direction='inbound')`：

- `FeishuWSClient._on_sdk_message()`
- `scheduler_loop()`
- `SubAgentPool._run_one()`

### 入站处理流

```text
来源
  -> RouteEnvelope(direction='inbound')
  -> Router.submit()
  -> normalize()
  -> route_inbound()
  -> adapt_to_inbox()
  -> InboxHub.submit(InboxEvent)
  -> ResidentConsumer
```

### route_inbound 职责

`route_inbound()` 负责：

- 校验 `target_addr`
- 校验 `kind`
- 做必要的 target 默认化
- 记录审计 / 摘要
- 调 `adapt_to_inbox()`

### adapt_to_inbox 职责

`adapt_to_inbox()` 明确表示：

- `InboxEvent` 不再是正式统一消息模型
- 它只是 `Router` 入站下游队列模型

推荐映射关系：

```python
InboxEvent(
    target=envelope.target_addr,
    kind=envelope.kind,
    payload=envelope.payload,
    source=envelope.source_addr or envelope.meta.get("source", ""),
    summary=envelope.summary,
)
```

## 出站语义

### 出站来源

以下来源统一构造 `RouteEnvelope(direction='outbound')`：

- `TurnResponder.reply_current()`
- `TurnResponder.send_current()`
- `TurnResponder.send_images()`
- `TurnResponder.notify()`
- 未来其它主动通知逻辑

### 出站处理流

```text
Presenter / TurnResponder / 主动通知
  -> RouteEnvelope(direction='outbound')
  -> Router.submit()
  -> normalize()
  -> route_outbound()
  -> deliver_outbound()
  -> Channel
  -> ChannelIO
```

### route_outbound 职责

`route_outbound()` 负责：

- 解析 `to`
- 处理默认 `source_addr`
- 挂 ACL / rate limit / audit / mirror
- 合并 `Channel defaults + Endpoint overrides`
- 继续保留 reply/send/image/reaction 判定语义

### deliver_outbound 职责

`deliver_outbound()` 负责：

- 对每个目标地址选择 `Channel`
- 实际调用 `Channel.send()` / `reply()` / `send_images()` / reaction 扩展
- 保留当前出站治理逻辑

## 兼容策略

### InboundDispatcher

`InboundDispatcher` 不再是并列正式入口。

建议降级为兼容壳：

- 保留现有 API 以降低迁移风险
- 内部仅负责把旧参数映射成 `RouteEnvelope(direction='inbound')`
- 最终调用 `Router.submit(...)`

也就是说，它的存在只为了兼容，不再代表主架构。

### UnifiedInboundEvent

建议迁移顺序：

- 第一阶段保留
- 作为兼容壳输入
- 由 `InboundDispatcher` 映射为 `RouteEnvelope`
- 主路径稳定后删除

### OutboundEnvelope

建议迁移顺序：

- 第一阶段保留
- `Router.send_envelope()` 继续存在
- 内部映射成 `RouteEnvelope(direction='outbound')`
- 主路径稳定后删除或保留为兼容别名

### InboxEvent

保留，但角色变化：

- 不是统一消息模型
- 只是 `Router` 入站下游队列对象

## 迁移顺序

### Phase 1：引入总模型

- 新增 `RouteEnvelope`
- `Router` 新增 `submit()` / `normalize()` / `route_inbound()` / `route_outbound()`
- 暂不删除 `InboundDispatcher` / `OutboundEnvelope`

### Phase 2：迁移入站主路径

- `FeishuWSClient` 改走 `Router.submit(inbound)`
- `scheduler` 改走 `Router.submit(inbound)`
- `subagent_pool` 改走 `Router.submit(inbound)`
- `InboundDispatcher` 降级为兼容壳

### Phase 3：迁移出站主路径

- `TurnResponder` 改为构造 `RouteEnvelope(direction='outbound')`
- `Presenter` 继续只依赖 `Router`
- `Router.send_envelope()` 内部兼容映射到统一模型

### Phase 4：清理旧模型

- 删除 `UnifiedInboundEvent`
- 删除 `InboundDispatcher` 的主入口地位
- 评估删除或别名化 `OutboundEnvelope`
- 更新注释与架构文档

## 风险

### 主要收益

- 外部世界只认一个 `Router`
- 真正形成“所有消息都走 Router”的统一主干
- 统一治理逻辑只挂一处
- 后续多通道扩展不会再复制另一套入口体系

### 主要风险

- 统一模型字段若设计过窄，后续还会重复改
- 入站 target 规则若处理不当，会把消息投错队列
- 兼容层若保留过厚，会让双模型状态拖太久
- 若 `InboxEvent` 被继续当正式模型使用，会重新长出第二套语义

## 测试要求

至少覆盖以下场景：

1. `Router.submit(inbound)` 能正确投递 `InboxEvent`
2. 飞书入口通过 `Router.submit(inbound)` 入队
3. 调度器通过 `Router.submit(inbound)` 入队
4. 子任务完成通过 `Router.submit(inbound)` 入队
5. `Router.submit(outbound)` 能继续正确发送文本
6. `reply_to` 语义不回归
7. `images` 路由不回归
8. `reaction` 代理不回归
9. `InboundDispatcher` 兼容层仍可工作
10. `send_envelope()` 兼容层仍可工作

## 文档对齐

本次设计落地后应同步更新：

- `docs/current-architecture-state.md`
- `docs/channels_and_router.md`
- `src/agent_core/channel/interfaces.py`

更新重点：

- 不再描述为“入站 Dispatcher + 出站 Router”
- 改为“单 Router + RouteEnvelope + Inbox/Channel 双下游”

## 设计决策摘要

- 采用单 `Router`
- 采用单统一模型 `RouteEnvelope`
- 入站和出站共用 `Router.submit(...)`
- `InboxHub` 退为 Router 入站下游
- `Channel` / `ChannelIO` 退为 Router 出站下游
- `reply_to`、`images`、`reaction` 继续保留
- 兼容迁移 `UnifiedInboundEvent` 和 `OutboundEnvelope`
- 本次不重写 `ResidentConsumer`、`InboxHub` 和 session target 归并逻辑

## 自检

- 无 `TODO`、`TBD`、占位段落
- 目标、非目标、数据模型、职责分层、兼容策略、迁移顺序、测试要求已闭环
- 与用户确认一致：不是“单 Router + 双模型”，而是“单 Router + 单模型”
- 没有把消费层重写混进本次范围，范围仍可控
