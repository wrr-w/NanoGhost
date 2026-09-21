# NanoGhost Current Architecture State

## 一句话

当前仓库已经形成：

- 统一主入口：`RouteEnvelope -> Router.submit(...)`
- 入站下游：`Router -> InboxEvent -> InboxHub -> ResidentConsumer`
- 出站下游：`Router -> Channel -> ChannelIO`

## 总体主干

```text
外部来源
  ├─ Feishu SDK 回调
  ├─ Scheduler 到点任务
  └─ SubAgentPool 完成回流
        ↓
RouteEnvelope(direction='inbound')
  -> Router.submit(...)
  -> InboxEvent
  -> InboxHub
  -> ResidentConsumer
  -> ws_client._handle_inbox_batch()
  -> run_agent_turn()
  -> Agent.chat_stream_events()
        ↓
TurnResponder
  -> RouteEnvelope(direction='outbound')
  -> Router.submit(...)
  -> ChannelManager
  -> ChannelRegistry / EndpointDirectory
  -> Channel
  -> ChannelIO
  -> Platform SDK / API
```

## 当前分层

### 1. Agent 核心层

- `src/agent_core/engine/agent.py`
- `src/agent_core/engine/messages.py`

职责：

- LLM 推理
- 工具调用
- 记忆注入与回合后处理
- 产出标准事件流

### 2. 统一路由层

- `src/agent_core/router.py`
- `src/agent_core/channel/route.py`

职责：

- 作为唯一正式消息入口
- 统一处理 `inbound / outbound`
- 统一承载 `RouteEnvelope`
- 出站以 `delivery + blocks` 表达一封完整信
- 对入站适配 `InboxEvent`
- 对出站投递 `Channel`

### 3. 入站消费层

- `src/agent_core/runtime/inbox.py`
- `src/agent_core/runtime/consumer.py`

职责：

- 按端点入队
- 端点内串行、端点间并发
- 由 `kind` 分派给具体处理逻辑

### 4. 编排层

- `src/agent_core/presenter.py`
- `src/agent_core/channel/responder.py`

职责：

- 消费 Agent 事件流
- 组装 `RouteEnvelope(direction='outbound')`
- 当前回复默认封成 `delivery='reply'`
- 不再直接执行 `ChannelIO`

### 5. 路由策略层

- `src/agent_core/channel/route.py`
- `src/agent_core/channel/manager.py`

职责：

- 定义统一路由模型
- 合并 `Channel 默认 + Endpoint 覆盖`
- 保留 `reply_to` 等平台语义的统一表达

### 6. 控制面与抽象层

- `src/agent_core/channel/base.py`
- `src/agent_core/channel/manager.py`
- `src/agent_core/channel/registry.py`
- `src/agent_core/channel/directory.py`
- `src/agent_core/channel/endpoint.py`

职责：

- 通道抽象
- 端点地址和目录
- 通道启停、健康、能力和投递策略
- 向上声明 `message_capability_profile()`

### 7. 平台实现层

- `src/agent_core/channel/feishu/channel.py`
- `src/agent_core/channel/feishu/io.py`
- `src/agent_core/channel/interfaces.py`

职责：

- 平台能力适配
- 平台 API 原语
- 维持飞书特性表达
- 根据平台能力把一封 blocks 信封落成一条或多条平台消息
- 飞书 `markdown` block 已接入原生 markdown / interactive 发送路径；遇到 `@人` 时自动降级为纯文本，保留 mention 语义

## 当前已经完成的收口

- 出站不再由编排层直接调用 `ChannelIO`
- 当前回复和主动通知统一走 `Router.submit(...)`
- 统一出站信封已支持 `delivery + blocks`，可表达有序 `text / markdown / image`
- `reply_to` 成为 Router 语义，而不是旧路径特例
- 飞书 SDK 回调、定时任务、子任务完成回流都直接走 `Router.submit(inbound)`
- 旧 `InboundDispatcher` / `OutboundEnvelope` 兼容层已移除
- `send_message` built-in 已升级为 Agent-facing 统一发信入口，底层仍走 Router
- 飞书 `markdown` 已不是“只声明能力”，而是实际可经 `send_message -> Router -> FeishuChannel/IO` 原生发出

## 当前尚未完全统一的地方

- 单 Router 已覆盖主要入口，但还没有扩展到更多通道类型
- session target 规则仍沿用现有逻辑，没有在本次重写
- 文档设计稿中仍有部分章节超前于当前实装

## 回归状态

- 单 Router 主链相关测试通过
- `reaction` 环境依赖测试未纳入本轮
- 若 `MCP client` 历史测试仍失败，视为本轮外问题
