# NanoGhost Unified Outbound Router Design

**Date:** 2026-09-20

**Status:** Proposed

**Goal**

把当前出站链路统一收口到 `Router`，让编排层不再直接依赖 `ChannelIO`；同时保留 `reply_to` 语义，不因统一路由而削弱飞书等聊天通道的能力表达。

## 背景

当前仓库已经具备以下基础：

- `Agent` 核心循环不直接依赖 `ChannelIO`
- `Router` 已经面向 `ChannelRegistry` 和 `Channel`
- `Channel` 与 `ChannelIO` 已经分层
- `TurnResponder` 已把 `Presenter` 中散落的 I/O 调用收敛了一层

但当前出站链路仍是混合态：

- 主动外发：`Router -> Channel -> ChannelIO`
- 当前轮回复：`Presenter -> TurnResponder -> ChannelIO`

这意味着：

- 出站没有单一正式入口
- `reply` 仍是编排层直连 I/O 的特例
- `Presenter` 仍感知平台 I/O 原语

本设计的目标不是“删掉 reply”，而是把 `reply` 变成 `Router` 统一出站模型中的可选语义字段。

## 设计结论

采用以下方案：

- 所有出站统一走 `Router`
- `Router` 引入统一 `OutboundEnvelope`
- `OutboundEnvelope` 支持 `reply_to`
- `Channel` 默认能力与 `Endpoint` 覆盖策略共同决定是否使用 `reply_to`
- `Presenter` 和 `TurnResponder` 不再直接使用 `ChannelIO`
- `ChannelIO` 下沉为 `Channel` 内部实现细节

## 非目标

本次设计不做以下事项：

- 不统一入站路由，入站仍保持现有 `ws_client / consumer / presenter` 主链
- 不删除 `ChannelIO`
- 不把所有平台能力压缩成最低公分母接口
- 不立即重构所有 `image / reaction / update / card` 为完整 envelope 子类型
- 不改动 `Agent` 的推理与工具调用模型

## 目标架构

### 目标分层

```text
Agent
  -> 产出标准事件

Presenter
  -> 将事件转成 OutboundEnvelope
  -> 调用 Router.send_envelope(...)

Router
  -> resolve target
  -> merge channel defaults + endpoint overrides
  -> decide reply/send
  -> dispatch to Channel

Channel
  -> 实现统一发送语义
  -> 内部调用 ChannelIO

ChannelIO
  -> 调用飞书/微信/其他平台 API
```

### 关键边界

- `Agent` 不感知 `ChannelIO`
- `Presenter` 不感知 `ChannelIO`
- `Router` 不直接面向 `ChannelIO`
- `Router` 直接面向 `Channel`
- `ChannelIO` 只存在于通道适配实现内部

## 数据模型

### OutboundEnvelope

统一出站模型建议引入如下结构：

```python
@dataclass
class OutboundEnvelope:
    to: list[str]
    text: str = ""
    source_addr: str = ""
    agent_key: str = "default"
    reply_to: str | None = None
    images: list[str] | None = None
    update_of: str | None = None
    reaction: dict | None = None
    meta: dict[str, Any] | None = None
```

第一阶段只要求稳定支持：

- `to`
- `text`
- `source_addr`
- `agent_key`
- `reply_to`

其余字段先保留扩展位，不要求一次性全部切完。

### Channel 默认策略

建议 `Channel` 提供默认发送策略，至少包含：

```python
{
  "supports_reply": True,
  "default_allow_reply": True,
  "default_prefer_reply": True,
}
```

说明：

- `supports_reply`：该通道技术上是否支持 reply
- `default_allow_reply`：默认是否允许 reply
- `default_prefer_reply`：在允许时是否优先 reply

### Endpoint 覆盖策略

建议在 `Endpoint.meta` 中增加可选策略字段：

```python
{
  "allow_reply": True | False | None,
  "prefer_reply": True | False | None,
}
```

覆盖规则：

- `Endpoint` 显式设置时，覆盖 `Channel` 默认值
- `Endpoint` 未设置时，回退到 `Channel` 默认策略

这样可以满足以下场景：

- 聊天端点：允许并优先 `reply`
- 工单端点：不允许 `reply`
- 事件源端点：通常不需要 `reply`

## Router 判定规则

Router 在处理 `OutboundEnvelope` 时按以下顺序判定：

1. 解析 `to -> targets`
2. 检查 `ACL / rate / endpoint enabled / channel enabled / capability`
3. 对每个 target 读取 `Channel defaults + Endpoint overrides`
4. 若满足以下全部条件，则走 `reply`
   - `envelope.reply_to` 不为空
   - `channel.supports_reply == True`
   - `resolved_allow_reply == True`
   - `resolved_prefer_reply == True` 或策略要求优先回复
5. 否则走普通 `send`

推荐把逻辑显式表达成：

```python
if envelope.reply_to and resolved.supports_reply and resolved.allow_reply:
    if resolved.prefer_reply:
        channel.reply(envelope.reply_to, envelope.text)
    else:
        channel.send(target, envelope.text)
else:
    channel.send(target, envelope.text)
```

### 为什么保留 `reply_to`

因为统一到 `Router` 不等于统一成“只会向 endpoint 发送一条新消息”。

`reply` 是一种路由语义，不是旧路径遗留物。只要 `Router` 的 envelope 能表达：

- 目标地址
- 回复目标消息

那么 reply 就仍然属于统一路由的一部分，而不是架构例外。

## 组件去留

### Router

保留，并扩展为统一出站正式入口。

新增建议：

- `send_envelope(envelope: OutboundEnvelope) -> dict`
- `deliver_envelope(envelope: OutboundEnvelope) -> dict`

现有 `send()` 可以继续保留，作为 envelope 的轻量包装或兼容入口。

### TurnResponder

保留，但职责变化。

从：

- 直接调用 `io.reply()`
- 直接调用 `io.send_text()`
- 直接调用 `io.send_images()`

改为：

- 只负责从“当前轮事件”构造 `OutboundEnvelope`
- 调用 `Router.send_envelope(...)`

也就是说，它从 I/O 执行器，变成当前轮出站封装器。

第一阶段不建议立即改名，避免同时做行为迁移与概念迁移。

### Channel

保留为 Router 的直接下游。

需要增强：

- 增加默认策略声明接口
- 明确 `reply()` 为标准能力之一
- 保留现有扩展能力，例如 `send_images()`、reaction 等

### ChannelIO

保留，但继续下沉。

定位不变：

- 平台 I/O 原语层
- 仅由 `Channel` 内部使用
- 不再被 `Presenter / TurnResponder` 直接依赖

## 飞书能力保留原则

本设计不允许因统一路由而削弱飞书能力。

必须保留的硬能力：

- `send_text`
- `reply`
- `send_images`
- `add_reaction / delete_reaction`
- `download_image`

建议继续保留的扩展能力：

- `update`
- `thread`
- `mention`
- `card`

策略原则：

- 统一控制流
- 不统一削弱能力
- 平台细节隐藏，但平台能力表达保留

## 兼容策略

### 兼容目标

- 不破坏现有 `FeishuIO`
- 不破坏现有 `FeishuChannel`
- 不破坏现有 `Router.send(...)`
- 逐步迁移 `Presenter -> TurnResponder -> ChannelIO` 链路

### 分阶段迁移

#### Phase 1

- 新增 `OutboundEnvelope`
- Router 支持 envelope 发送
- Endpoint/Channel 增加 reply policy
- 保持旧接口不删

#### Phase 2

- `TurnResponder` 改为构造 envelope
- `Presenter` 改为只依赖 `Router`
- 当前回复链退出 `ChannelIO`

#### Phase 3

- 审查所有直接 I/O 调用点
- 清理编排层对 `ChannelIO` 的剩余直接依赖

#### Phase 4

- 再决定是否把 `image/reaction/update` 完全 envelope 化

## 测试要求

至少覆盖以下场景：

1. `reply_to + allow_reply=true + supports_reply=true` 时走 `Channel.reply`
2. `reply_to + endpoint allow_reply=false` 时退化到 `Channel.send`
3. `reply_to + channel supports_reply=false` 时退化到 `Channel.send`
4. 无 `reply_to` 时走 `Channel.send`
5. `Presenter` 不再依赖 `ChannelIO`
6. 现有 `send_message` 路由行为不回归
7. 飞书 `reply/send_text/send_images/reaction` 旧能力不回归

## 风险

### 主要收益

- 所有出站有统一正式入口
- reply 从旧路径特例变成 Router 语义
- 编排层退出 `ChannelIO`
- 多通道边界更稳定

### 主要风险

- 当前回复链回归风险高于普通主动外发
- 若 envelope 设计过窄，后续 image/update/reaction 会重复再改一次
- 若 policy 合并逻辑不清晰，可能导致聊天端点异常退化成普通 send

## 设计决策摘要

- 采用方案 A：统一出站 Router
- 保留 `reply_to`
- 使用 `Channel defaults + Endpoint overrides`
- Endpoint 覆盖 Channel 默认值
- `TurnResponder` 保留，但改为 envelope 封装层
- `ChannelIO` 不再允许被编排层直接使用

## 实施建议

下一步应生成一份实现计划，按以下顺序展开：

1. 定义 `OutboundEnvelope`
2. 扩展 `Router` 为 envelope 模式
3. 扩展 `Channel` 默认 reply policy
4. 扩展 `Endpoint` reply policy
5. 改造 `TurnResponder`
6. 改造 `Presenter`
7. 回归飞书能力与相关测试

## 自检

- 无 `TODO`、`TBD`、占位段落
- 目标、非目标、架构、数据模型、判定规则、兼容策略、测试要求已闭环
- 设计没有要求删除 `reply`，与用户确认保持一致
- 范围聚焦于统一出站 Router，没有把入站统一混进本次设计
