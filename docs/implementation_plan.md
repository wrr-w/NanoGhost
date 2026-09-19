# 分段实现计划（v1 · 设计 → 代码）

> 依据：`docs/channels_and_router.md`（设计 v2）+ `docs/arch_final.png`（全景图）。
> 状态：**计划稿，未动代码**。

---

## 0. 总原则

```
① 增量、向后兼容：只「加」，不改主流程；旧路径（飞书 WS → run_agent_turn）始终可用
② 每段【可独立验收】；每段结束出【总结 + review】
③ 同一个 asyncio loop；不引外部 broker（进程内总线）
④ 不破坏打包：新模块进 build.spec hiddenimports
```

## 每段固定交付格式

```
### 阶段 PX · 名称
- 做了什么（新增/改动 文件清单）
- 验证结果（跑通的用例 + 实际输出）
- 与设计偏差（如有，为什么）
- 遗留 / 下一段前置
```

---

## 阶段划分

### P0 · 通道抽象 + 注册表 + 端点目录（地基）
**目标**：任何东西（人 / 群 / 事件源 / agent）都能**注册成一个端点**，可枚举。

**新增**
- `src/agent_core/channel/base.py` — `Channel` 协议（`send/reply/update/capabilities/start`）
- `src/agent_core/channel/registry.py` — 适配器注册表（channel type → adapter）
- `src/agent_core/channel/endpoint.py` — `Endpoint` 模型（addr/channel/kind/capabilities/status/meta）
- `src/agent_core/channel/directory.py` — 端点目录（动态注册 / 查询 / 生命周期）

**改动**
- `channel/interfaces.py`、`channel/instance.py` — 泛化对齐
- `channel/feishu/*` — 适配成 `Channel` 实现（补 `update` / `capabilities`）
- `run.py` — 启动时注册 feishu 适配器 + 静态端点

**验收**
- [ ] 飞书端点（user / group）注册进目录，`directory.list()` 可查
- [ ] `channels.capabilities(addr)` 返回正确能力
- [ ] 单测脚本通过；**旧飞书路径不破**

**风险**：低　　**预估**：~300 新增 / ~80 改

---

### P1 · 每会话队列 + 常驻消费者
**目标**：所有入口（通道消息 / 事件）先入「每端点队列」；消费者按忙闲唤醒。

**新增**
- `src/agent_core/runtime/queue.py` — 端点队列（FIFO · 不可丢 · notify）
- `src/agent_core/runtime/consumer.py` — 常驻消费循环（忙闲判定、批量 drain）

**改动**
- `run.py` — `gather(ws.run_forever, consumer_loop, ...)`
- `presenter.py` / `engine/agent.py` — 暴露 per-endpoint 「running」标志
- `scheduler.py` — 收编为「事件生产者」（丢事件，不再自己起轮）

**验收**
- [ ] 消息入队 → 消费者唤醒 → 一轮处理
- [ ] 同一端点忙时**排队**；不同端点**并发**
- [ ] 定时任务改为「丢事件」

**风险**：中（控制流）　　**预估**：~250 新增 / ~80 改

---

### P2 · 路由（模型自由 + 护栏）+ send 工具 + 出站镜像
**目标**：模型能**自由把消息发到任意端点**；有护栏；发出的也写进目标会话。

**新增**
- `src/agent_core/router.py` — `resolve`（explicit / reply / default） + `deliver`（fan-out） + ACL / 防环 / 限速 / 审计
- `src/agent_core/tool/builtins/send.py` — `send(target, text)` 工具
- 出站镜像 hook — 发送路径统一 → 写**目标会话**（复用 `database.add_agent_message`）

**改动**
- `presenter.py` — 出站走 router
- `adapters/database.py` — 复用写目标会话

**验收**
- [ ] 模型 `send(to=["feishu:ou_X"])` 成功投递
- [ ] 没说目标 → **默认回来源**
- [ ] ACL 拦截越权；防环 / 限速生效
- [ ] **出站镜像**：目标端点会话历史里有这条

**风险**：中　　**预估**：~330 新增 / ~60 改

---

### P3 · 子 Agent 池（后台 / 并行 / 上限 / 回报）
**目标**：`delegate` 变后台、可并行、有上限（默认 3）、跑完回报主 agent。

**新增**
- `src/agent_core/runtime/subagent_pool.py` — Semaphore(N) + FIFO + 运行表（内存）+ 完成事件

**改动**
- `tool/builtins/delegate.py` — 接池；`background` 默认真
- `engine/agent.py` — ReAct 边界 drain 完成事件（注入）
- `run.py` — pool 绑定主 loop

**验收**
- [ ] `delegate_task(background=True)` 立即返回 `run_id`
- [ ] 并行上限 3，超出**排队**
- [ ] 完成 → 运行表 + 事件 → 主 agent 边界看到

**风险**：中-高（asyncio）　　**预估**：~230 新增 / ~130 改

---

### P4 · 驱动（Watcher）+ Sink + 通道管理补全
**目标**：Watcher 感知变化；结果落 sink；通道管理补健康 / ACL / 限速 / 观测。

**新增**
- `src/agent_core/runtime/watcher.py` — 轮询 + 快照比对 → 事件
- `src/agent_core/sink.py` — 结果 / 事件出口
- 通道管理接口 — `health / acl / rate / metrics`

**改动**
- `scheduler.py`（Timer 生产者）
- 工具：`list_endpoints()` 等暴露给模型

**验收**
- [ ] Watcher 检测工单 / 信号变化 → 事件 → 唤醒
- [ ] Sink 落库；渠道订阅
- [ ] 通道管理接口可查

**风险**：中　　**预估**：~260 新增 / ~60 改

---

## 里程碑

```
P0 + P1 + P2  =  MVP（多通道 + 端点 + 模型自由发送）
P3 + P4       =  完整
```

## 每段 Review 检查点

```
1. 验收清单全过？
2. 旧路径（飞书对话）未破？
3. 打包通过（build.spec）？
4. 与设计文档 `channels_and_router.md` 一致？
5. 遗留问题登记？下一段前置就绪？
```

## 总预估

```
新增 ~1,600–1,900 行 · 改动 ~400–500 行
MVP(①②③) ≈ 3–5 天 ｜ 完整 ≈ 1.5–2.5 周
风险集中：控制流改造、asyncio 绑定
```

---

## 进度

| 阶段 | 状态 | 备注 |
|------|------|------|
| P0 通道抽象 + 注册表 + 端点目录 | ✅ 完成 | `channel/base.py · registry.py · endpoint.py · directory.py · feishu/channel.py`；5 用例 |
| P1 每会话队列 + 常驻消费者 | ✅ 完成 | `runtime/inbox.py · consumer.py`；ws_client 入队；scheduler 改丢事件；8 用例 |
| P2 路由 + send 工具 + 出站镜像 | ✅ 完成 | `router.py · builtins/send.py`；ws_client 挂镜像；12 用例 |
| P3 子 Agent 池 | ✅ 完成 | `runtime/subagent_pool.py`；delegate 默认后台；边界注入；8 用例 |
| P4 驱动(Watcher) + Sink + 通道管理补全 | ✅ 完成 | `runtime/watcher.py · sink.py · channel/admin.py · builtins/endpoints.py`；7 用例 |

> 每段完成后出【总结 / review】；打包统一留到最后（后续阶段不单独打包）。
>
> **P0–P4 全部完成。** 新增 ~14 文件 + 5 测试；`tests/test_channel_p0..p4` = 40 passed；全量 117 passed / 15 既有失败（环境依赖，与本工作无关）。

