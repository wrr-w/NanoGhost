# 多通道 · 端点 · 路由 · 通道管理 —— 架构设计 v2

> 状态：**部分落地，部分仍为设计**。
> 已落地：端点地址模型、端点目录、通道注册表、ChannelManager、单 Router 主入口、RouteEnvelope、InboxHub、ResidentConsumer。
> 部分落地：飞书通道适配、子任务完成回流、定时任务入站统一、`delivery + blocks` 出站信封、Agent-facing `send_message` tool、飞书原生 `markdown` 发送。
> 未完全落地：更多通道扩展、统一 session target 规则、更多平台级治理接口。

---

## 0. 一句话（三层总纲）

```
① 传输层（通用、与 Agent/场景无关）：端点 · 消息 · 路由 · 通道管理
② 消费层：每会话队列 + 常驻消费者（忙则排队、闲则唤醒）
③ 目标层：工单 / 任务（持久化「目标+状态进展」，负责跟踪与收敛）
```

---

## 1. 概念辨析：通道 vs 端点 ★

### 1.1 三个名字，别混

| 名字 | 是什么 | 例 |
|------|--------|-----|
| **通道类型 / adapter** | 一种收发**实现**（协议/平台级）| `feishu` · `email` · `event` · `cli` |
| **通道** | 一个**平台/账号级**的接入 | `feishu`（一个机器人/账号）|
| **端点 Endpoint** | 一个**可寻址的目的地**（最小粒度）| `feishu:ou_A`（a 用户）· `feishu:oc_B`（某群）|

> **一句话**：**通道是「一种接入方式」，端点是「一个具体收发对象」。**
> 同一通道下，**a 用户、b 用户 各是一个独立端点**。

### 1.2 为什么用「端点」统一（核心）

```
人（用户 a / 用户 b）· 群 · 通知渠道 · agent · 事件源 · 子 agent run · 定时任务
   ↑ 这些【全都是端点】—— 同一层、同一套收发
```
- **扁平统一**：不再分「人 / 群 / 服务 / 事件」各种型，**一律是端点**；
- **channel 降级成「命名空间 / 类型前缀」**：`feishu:` 只是地址的一部分，不是"层级"。

### 1.3 地址

```
Address = "channel:target"            # 字符串，可配置/日志/入库
  feishu:ou_A     一个人（端点）     ← 你要的粒度
  feishu:oc_B     一个群（端点）
  event:src1      一个事件源（端点）
  agent:sess-X    一个 agent 会话（端点）
  subagent:run-9  一个子 agent 运行（端点）
```

### 1.4 粒度对比：我们 vs OpenClaw

| | OpenClaw | **我们** |
|---|---|---|
| 主单位 | **channel（平台）** | **endpoint（任何可寻址物）** |
| 层级 | channel → account → peer → thread（**分层 + 分型**）| **扁平一层**：`channel:target` |
| 用户级 | 有（peer），但是**子级** | **一等公民**（a/b 用户各是端点）|
| 路由单位 | channel/account/peer → **选一个 agent** | **endpoint → endpoint**（模型自由选）|

→ **我们不是"更细一点"，是"更细 + 更通用"**：把不同类的东西统一到端点层。

### 1.5 忙闲 / 并发的单位

```
端点（= 目的地）：端点内【串行】（忙则排队），端点间【并发】
  同群多人同时说 → 串行；不同群/不同人 → 并发
```

---

## 2. 总架构图（全景）

```
外部/内部
 ├─ endpoint feishu:ou_A ┐
 ├─ endpoint feishu:oc_B ┼─(直连/注册)─► 每会话队列 ──► 常驻消费者 ──► 主 Agent
 ├─ endpoint event:src   ┘   ▲                          ▲              │
 └─ endpoint agent:...       │                          │              │
                             │                    (闲则唤醒)           │
                             └──── 事件回流 ◄── 子任务完成 / 下游结果 ◄──┤
                                                                       │
                              Router（模型自由选目标 + 护栏）◄── 语义目标 ─┘
                                    │
                                    └── deliver ──► 各端点（人 / 群 / 服务 / agent）
   驱动：Timer · Watcher · 子任务完成 · 手动   ──► 丢事件回队列（闭环）
   Sink（B）：结果 / 事件 → 落库 / 总线（渠道是订阅者）
```

---

## 3. 端点与消息

### 3.1 端点（Endpoint）

```
Endpoint {
  addr        "channel:target"      # 唯一地址
  channel     "feishu"              # 命名空间/类型
  kind        user | group | service | agent | event | ...   # 仅作筛选，不作分支
  capabilities { text, markdown, card, mention, update, media }  # 能干什么
  status      online | offline | degraded
  meta        { 显示名、所属、限速、ACL… }
}
```

### 3.2 消息（信封固定 · 内容任意）

```
Message {
  # —— 信封（传输层认识）——
  id, ts, from(addr), to(addr[] 0..N),
  delivery(reply|send), summary?, reply_to?,
  # —— 内容（统一 blocks）——
  blocks[
    {type=text, text},
    {type=markdown, text},
    {type=image, images[]},
    ...
  ],
  payload
}
```
- **信封统一、内容任意**（类比邮件：SMTP 标准信封，正文随便）。
- 当前主干已经从扁平 `text/images` 走向 `delivery + blocks`，一封信可表达有序图文。
- **收件人 = 列表(0..N)**：1 个 = 一对一，N 个 = 一对多（fan-out）。

---

## 4. 路由：模型自由 + 护栏 ★

### 4.1 取向（这是我们的关键选择）

```
OpenClaw：宿主配置说了算 ·「模型不选通道」· 默认只回来源   ← 收紧（IM/客服）
我们    ：模型按语义【自由选端点/多目标】                  ← 放开（编排/协作）
→ 不是对错，是场景不同。我们要的就是「自由」。
```

### 4.2 解析（模型给「语义目标」，Router 落成地址）

```
send(to="reply")     → 回来源（默认兜底）
send(to=["feishu:ou_A","feishu:oc_B"]) → 模型直接点名多个端点（自由）
（没说）              → 默认回来源
```

### 4.3 自由 ≠ 无约束 → 用**护栏**，不用锁死

```
· 可见性/ACL ：这个场景能发给哪些端点（白名单/能力）
· 防环       ：消息带 hop/来源，转过的别再转回来
· 限速/防刷屏：同内容冷却 + 每窗口上限
· 审计       ：谁发的 · 发给谁 · 可回溯
· 默认兜底   ：模型没说 → 回来源
```

### 4.4 场景策略 = **辅助**，不是锁

```
场景策略（工单/任务/值班）= 给默认值 / 建议候选 / 兜底
   → 模型可采纳、可无视（保持自由）
```

---

## 5. 通道管理 ★

**目的**：管「**有哪些端点、它们是什么、能不能用、给不给我用**」—— 这也是**模型自由路由的"菜单"来源**。

### 5.1 要管什么（10 项）

| # | 能力 | 说明 |
|---|------|------|
| 1 | **注册（Registry）** ★ | **两类都注册**：① **适配器**（通道类型 `feishu/email/event`）② **端点**（实例 `feishu:ou_A`）|
| 2 | **端点目录（Directory）** | **注册表的视图**：已注册端点清单（地址/显示名/所属/状态）→ 模型自由路由的「菜单」|
| 3 | **能力（Capabilities）** | 支持 text/card/mention/update/media → 决定"能不能发卡片/原地更新" |
| 4 | **健康（Health）** | 连着没 / 延迟 / 最近错误（token、cookie 到期）|
| 5 | **启停（Enable/Disable）** | 启用/停用某通道或端点 |
| 6 | **配置/认证（Config/Auth）** | token、webhook、机器人身份、账号 |
| 7 | **可见性（ACL）** | 某场景/agent 能发给哪些端点（白名单）|
| 8 | **限速/配额** | 每端点/每通道发送频率上限 |
| 9 | **观测** | 消息量 / 错误率 / 延迟 / 活跃端点 |

### 5.2 接口概念（不写实现）

```
channels.register(adapter)                     # 1 注册
directory.list(kind?, q?) -> [Endpoint]        # 2 目录 ★（模型自由路由的菜单）
directory.get(addr)      -> Endpoint
channels.capabilities(addr) -> {...}           # 3 能力
channels.health(addr)    -> {ok, latency,...}  # 4 健康
channels.enable(addr, on) -> bool              # 5 启停
channels.acl(agent)      -> [addr]             # 7 可见性（**按 agent 授权**）
channels.rate(addr)      -> {limit, used}      # 8 限速
channels.metrics(addr)   -> {sent, recv, err}  # 9 观测
```

### 5.3 和「模型自由路由」的关系（要点）

```
模型要"自由选目标" → 必须先"看得见有哪些目标"
   list_endpoints() / directory.list()  ← 通道管理提供的【菜单】
   → 模型从菜单里选（自由），而不是自己编地址（失控）
   → ACL/限速 兜住风险
```

### 5.4 注册 —— 端点的「存在方式」 ★

**没注册的端点 = 不可寻址 = 发不过去。注册 = 接入。**

```
① 适配器（通道类型）  feishu / email / event        ← 代码 / 插件注册
② 端点（实例）        feishu:ou_A / event:src1       ← 挂在该适配器下
                      agent:sessX / subagent:run-9
```

**端点注册的来源**
```
静态：配置 / 启动时声明（机器人、通知渠道、agent、事件源）
动态：· 收到某端点来的消息 → 自动登记（「第一次见就注册」）
      · 被拉进新群        → 群端点注册
      · agent / 子agent / 任务 创建 → 注册成端点
```

**注册项**
```
register(addr, channel, kind, capabilities, meta)
  addr=channel:target · kind=user|group|service|agent|event|task
  capabilities={text,card,mention,update,media} · meta=显示名/ACL/限速/handler
```

**生命周期**
```
register → online → (disable / offline) → unregister
（删号 / 群解散 / 任务结束 → 标 offline / stale，别硬删，留审计）
```

**和目录的关系**
```
注册 = 动作（谁把自己登记进来）
目录 = 结果（注册表的视图）← 模型自由路由的「菜单」
→ 不是「先有目录再填」，而是「端点注册后，目录里才有它」
```

---

## 6. 会话 / 每会话队列 / 常驻消费者

```
端点（目的地）↔ 会话（上下文边界）—— 粒度一致
  · 每会话一个【入站队列】（FIFO · 不可丢；≠ 缓存）
  · 常驻消费者：与渠道 WS 并列的 asyncio 协程
      会话闲 → 起一轮；会话忙 → 留给该轮的 ReAct 边界注入
  · 事件：信封统一、内容任意；由「超时兜底 + 事件唤醒」驱动
```

### 6.2 出站镜像（outbound mirroring）★

```
agent / 任务 主动把消息发到「别的端点」时
   → 这条消息【也要写进「目标端点」的会话历史】
     （不是只写进「当前会话」）

例：agent 在会话 A 里，主动给群 X 发了通知
   → 群 X 的会话记录里必须有这条
     （否则下次有人在群 X 回复，agent 手里的群 X 上下文缺了「自己刚发过的」→ 对不上）
```

- **由框架自动做**（不靠模型、不靠调用方手动记）→ 统一放在「发送」那一步；
- 回来源（同一会话）时天然一致，无需特判；
- 现状：NanoGhost 会话历史存 `agent_messages(session_id)`，**只记当前会话**；
  目前**没有「发到别的会话」的工具**，所以还没暴露问题 ——
  **一旦加「自由路由 / 跨端点发送」，必须同步做镜像。**

---

## 7. 子 Agent 池（后台 · 并行 · 上限）

```
delegate_task(background=true) → 返回 run_id（立即）
SubAgentPool：asyncio.Task + Semaphore(N，默认 3) + FIFO 排队 + 运行表(内存)
跑完 → 写运行表 + 发事件（子任务完成）→ 回队列 → 唤醒主 agent
子 agent 继承父工具（含定时的 schedule_task 等）；上下文隔离（独立 session）
```

---

## 8. 驱动与 Sink（B）

```
驱动（事件产地）：Timer（interval/cron/at）· Watcher（工单/信号变化）· 子任务完成 · 手动
   全部 →「丢一条事件」→ 回【每会话队列】（唯一入口）→ 消费者唤醒
Sink（B：结果解耦）：任务/agent 的产出 = 结果/事件 → 落库/总线；渠道是订阅者
   （任务/agent 自己不发消息）
```

---

## 9. 与 OpenClaw / NanoGhost gateway 的关系

| | OpenClaw gateway | NanoGhost gateway | **我们的** |
|---|---|---|---|
| 本质 | **消息路由**（WebSocket server）| **进程守护**（WorkerManager）| **端点级消息路由 + 通道管理** |
| 粒度 | channel 为主（peer 为辅）| channel（一个 worker）| **endpoint（最细）** |
| 路由 | 配置（bindings）选 agent | 无 | **模型自由选 endpoint** |
| 通道管理 | 部分（channels/accounts 配置）| channel_directory.json + 开关 | **注册表+目录+能力+ACL+限速** |

**已采纳（与取向无关的工程件）**：会话 key / 每会话队列 / 去重 / 去抖 / **出站镜像**（→ §6.2）/ 分片。
**不借鉴**：「模型不选通道」（与我们要的自由路由相反）。

---

## 10. 落地顺序 / 待定

```
① 通道抽象 + 注册表 + 端点目录（先把飞书套成 端点）        ← 含 §5 的 1/2/3
② 路由（模型自由 + 护栏：ACL/防环/限速/默认兜底）          ← §4
③ 每会话队列 + 常驻消费者                                  ← §6
④ 子 Agent 池（后台/并行/上限/完成回报）                    ← §7
⑤ 驱动（Timer/Watcher）+ Sink                              ← §8
⑥ 通道管理补全（健康/启停/观测/别名分组）                   ← §5 的 4/5/9/10
```

**待定**：
1. 端点目录：**静态配置** 还是 **运行时动态发现**？
2. ACL 粒度：按「场景」还是按「agent」授权？
3. 别名/分组：放配置文件还是入库（可热改）？
4. 出站镜像：现在做，还是等 §6 一起？
