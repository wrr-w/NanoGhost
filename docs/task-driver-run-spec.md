# Driver + Task + Run Spec

## 1. 文档定位

本文档定义一个面向工程系统的 `Driver + Task + Run` 机制。

该机制的目标不是替代现有 Agent Run，而是在其之上提供一层：

- 可注册的驱动协议
- 可轮询、可激活的驱动调度机制
- 薄状态的 Task 对象
- 可触发、可审计、可回写的 Run 执行实例

本文档只定义机制，不预定义领域语义。

当前代码基线提交：

- `6179845` `feat(core): checkpoint current runtime and feishu flow changes`

---

## 2. 核心设计立场

### 2.1 框架只提供机制，不提供语义

框架不定义：

- 什么叫确认
- 什么叫价格穿越阈值
- 什么叫告警恢复
- 什么叫审批通过
- 什么叫满足执行条件

这些都属于：

- 用户定义
- 任务定义
- 驱动定义
- 领域事件定义

框架只负责：

- 让 Driver 可以被注册
- 让 Driver 可以被调度和激活
- 让 Task 可以被状态驱动
- 让 Task 在条件满足时触发 Run
- 让 Run 结果可以回写 Task

一句话：

- **Framework owns the mechanism; user owns the semantics.**

### 2.2 Driver evaluation 是主机制

本机制的核心不是“中心化事件匹配”，而是：

- Driver 被调度器周期性激活
- Driver 在激活时自行采集 / 检查 / 判断
- Driver 自己决定是否推进 Task 状态
- Driver 自己决定是否触发 Run

也就是说：

- 轮询 + 激活，本身就是一种采集
- 所谓“事件检测”，本质上也是 Driver 内部的一组条件判断

框架不试图替用户统一事件语义，只提供 Driver evaluation 的机制。

### 2.3 Task 必须打薄

Task 不是执行器，不是工作流定义，不是 Agent Prompt，也不是线程。

Task 只保存：

- 目标
- 约束
- 状态
- 驱动绑定
- 来源引用

Task 不保存：

- 具体执行步骤
- tool call 栈
- 线程/进程信息
- LLM 推理中间态
- 采集器内部实现

### 2.4 Run 是短生命周期执行实例

Run 代表一次具体执行。

Run 可以由：

- 主 Agent 执行
- 子 Agent 执行
- 固定 workflow 执行
- 外部系统执行
- 人工执行

Task 长期存在，Run 短暂存在。

### 2.5 Driver 是行为对象

Task 是状态对象。

Driver 是行为对象。

Driver 定义：

- 自己何时被激活
- 激活时如何采集 / 检查外部或内部信号
- 如何评估条件
- 何时推进 Task 状态
- 何时触发 Run
- 如何处理 Run 回写

### 2.6 DriverPump 是调度对象

Driver 不应自行常驻死循环。

框架提供 `DriverPump` 作为调度器，负责：

- 轮询已注册 Driver
- 按节奏激活 Driver
- 控制并发、超时、退避、预算

DriverPump 不解释业务语义，只负责把 Driver 叫醒。

---

## 3. 核心对象定义

## 3.1 DriverPump

DriverPump 是 Driver 的统一调度器。

职责：

- 维护已注册 Driver 列表
- 周期性或按计划激活 Driver
- 控制 Driver 的执行频率、并发和超时
- 防止 Driver evaluation 退化成高成本 busy loop

DriverPump 不负责：

- 解释业务语义
- 判断 Task 是否满足领域条件
- 决定 Task 的具体状态迁移
- 决定 Run 的业务内容

DriverPump 推荐最小控制字段：

- `min_interval`
- `next_tick_at`
- `backoff_policy`
- `max_concurrency`
- `timeout`
- `quota`

说明：

- DriverPump 只负责 “何时激活”
- Driver 自己负责 “激活后如何判断”

## 3.2 Event Package

事件不是静态消息，也不是框架预定义的固定几种类型。

事件是一个被包装的驱动单元。

事件至少由两部分组成：

- `Envelope`
- `Payload`

并且允许带：

- 匹配逻辑
- 提取逻辑
- 局部触发逻辑
- 路由提示

事件的核心不是“它长什么样”，而是：

- 它能否被用户定义
- 它能否在 Driver 激活时被检查
- 它能否被审计
- 它能否被版本化

### 建议的统一 Envelope 字段（可选）

- `event_name`
- `event_version`
- `source`
- `timestamp`
- `payload`
- `routing`
- `correlation_id`
- `tags`

说明：

- 统一的是信封
- 自定义的是 payload、事件语义以及局部逻辑

这里的 Event Package 不一定由中心化 Collector 推送而来。

在 Driver-Polling 模式下：

- Event Package 也可以由 Driver 在 `activate()` 内自行构造
- 即 “采集 + 包装 + 判断” 发生在 Driver 内部

### Event Package 可执行能力

Event Package 可以包含可执行逻辑，但必须是：

- 可注册
- 可审计
- 可版本化
- 可替换
- 可限制执行边界

而不是任意运行时拼接脚本。

建议能力：

- `match(task) -> bool`
- `extract(task) -> dict`
- `should_transition(task) -> bool`
- `build_trigger(task) -> dict`

## 3.3 Driver

Driver 是 Task 的驱动协议实现，也是条件判断逻辑的主要承载者。

Driver 不等于事件，不等于 Task，不等于 Run。

Driver 负责站在 Task 一侧进行 evaluation。

职责：

- 声明自己如何被激活
- 声明自己如何采集或读取输入
- 声明自己如何评估条件
- 声明自己如何推进 Task 状态
- 决定是否触发 Run
- 决定如何处理 Run 结果

Driver 可以理解为：

- Task 行为定义
- Task 驱动器
- Task 协议实现

Driver 可以是：

- 固定驱动器
- 规则驱动器
- 领域驱动器
- 用户定义驱动器

推荐 Driver 最小契约：

- `activate(now, context) -> DriverEvaluationResult`
- `list_tasks() / iter_tasks()`（可选）
- `evaluate(task, context) -> TaskDecision`（可内嵌进 activate）

也可以实现为：

- 激活一次后自行遍历绑定的 Task
- 对每个 Task 做条件判断
- 产生状态迁移和 Run 触发请求

## 3.4 Task

Task 是一个薄状态对象。

推荐最小字段：

- `task_id`
- `goal`
- `constraints`
- `state`
- `driver_ref`
- `source_ref`
- `owner_ref`
- `created_at`
- `updated_at`

字段语义：

- `goal`
  - 任务要完成什么
- `constraints`
  - 任务边界、条件、限制、预算、权限、确认要求等
- `state`
  - 当前业务状态
- `driver_ref`
  - 绑定哪个 Driver 处理该 Task
- `source_ref`
  - 该 Task 来源于哪个上游上下文、消息、系统或注册对象
- `owner_ref`
  - 谁拥有该 Task，可为空或指向用户/系统/租户

Task 不应绑定：

- 某个具体线程
- 某个具体 LLM 调用
- 某次 tool call
- 某种固定事件类型集合

## 3.5 Run

Run 是一次具体执行实例。

Run 的作用：

- 承接 Driver 触发出来的执行动作
- 占用实际运行资源
- 产出结果
- 回写 Task

推荐最小字段：

- `run_id`
- `task_id`
- `executor_ref`
- `input_snapshot`
- `status`
- `result`
- `error`
- `started_at`
- `finished_at`

Run 可以是：

- Agent Run
- SubAgent Run
- Workflow Run
- External Job Run
- Manual Run

### 3.5.1 Executor（执行器协议）

为了兼容 “Agent 执行” 与 “脚本/工作流执行”，框架将执行统一抽象为 `Executor` 协议。

框架不假设执行器一定是 LLM，不假设执行器一定在进程内，也不假设执行器必须同步返回。

推荐 `Executor` 最小契约：

- `start(run) -> handle`
  - 启动一次执行，返回一个可追踪的句柄（可以是 pid / job_id / token / 句柄对象）
- `get_status(handle) -> status`
  - 获取执行状态（可选；回调模型可不实现）
- `get_result(handle) -> RunResult`
  - 获取最终结果（可选；回调模型可不实现）
- `cancel(handle) -> bool`
  - 取消执行（可选）

执行器的实现形态允许：

- 同步执行（start 直接返回结果）
- 异步执行（start 返回 handle，后续 poll 或 callback）
- 外部系统执行（start 提交到外部系统，handle 为 external_job_id）

### 3.5.2 ExecutorRef（执行器引用）

`Run.executor_ref` 是对执行器的稳定引用。

`executor_ref` 的作用是：

- 将 “由谁执行 / 怎么执行” 从 Task 中解耦
- 支持动态替换执行方式
- 支持版本化与灰度升级

示例：

- `agent.main`
- `agent.research.v1`
- `script.local:python -m tools.run_tests`
- `workflow.temporal:build_pipeline_v2`
- `external.jenkins:job/xxx`
- `manual.approval_gate.v1`

### 3.5.3 ExecutorRegistry（执行器注册表）

框架需要一个可查询的执行器注册表：

- `executor_ref -> executor implementation`

注册表提供：

- 执行器能力描述（capabilities）
- 运行边界（timeout / quota / permissions）
- 版本信息

注册表允许：

- 代码内注册（插件式）
- 配置注册（YAML/DB）
- 运行时动态注册（受控）

### 3.5.4 Agent 作为执行器

当 Agent 作为执行器时，推荐模式是 “Agent Worker 消费 Run”：

- Task System 生成 Run（带 executor_ref = agent.*）
- Agent Worker 从 Run 队列/存储中取出待执行 Run
- 以 Run.input_snapshot / Task.goal / Task.constraints 组织一次 Agent Run
- 产生 RunResult 并回写 Task System

要点：

- Task System 不直接调用 Agent 的内部函数接口
- Agent 作为一个独立 worker 进程/服务接入系统
- Agent 是否派生 SubAgent 属于执行器内部实现细节，不属于 Task System 语义

### 3.5.5 Script / Workflow 作为执行器

当脚本/工作流作为执行器时，推荐模式是 “Job Worker 消费 Run”：

- Task System 生成 Run（带 executor_ref = script.* / workflow.*）
- 对应 Worker 启动外部进程或提交工作流
- handle = pid / job_id / external_job_id
- 执行完成后回写 RunResult

脚本执行器的输入也应来自 Run.input_snapshot，而不是直接解析 Task。

### 3.5.6 RunResult（执行结果）

Run 执行结束后，回写统一的 `RunResult`：

- `ok`
- `summary`
- `artifacts`（可选：文件、链接、结构化数据）
- `error`（可选）
- `metrics`（可选：耗时、成本、重试次数）

Driver 负责解释 RunResult 对 Task 状态的含义（done/failed/blocked/...）。

### 3.5.7 FeedbackMessage（反馈消息）

Run 的输出不应被限制为固定格式。反馈应类似“邮件”：

- 发件人（sender）
- 收件人（recipients）
- 内容开放（content open）
- 格式可由任务自定义（task-defined formatting）

框架只提供最小信封与投递机制，不预定义内容语义。

推荐最小字段：

- `message_id`
- `task_id`
- `run_id`
- `sender`
- `recipients`
- `subject`（可选）
- `content`（开放结构）
- `priority`（重要/紧急四象限）
- `delivery_policy`（投递策略）
- `created_at`

#### sender / recipients

- `sender`：发送方身份引用（user/agent/system/driver）
- `recipients`：收件方列表（user/agent/channel endpoint）

#### content（开放内容）

`content` 不应被框架限制为单一文本。建议支持多段内容与多种载体：

- text
- markdown
- card（例如飞书卡片）
- attachment（文件、链接、结构化数据）
- custom（任务自定义 payload）

框架只要求：

- `content` 可序列化
- `content` 可审计/可存档（至少存摘要与引用）

#### priority（重要/紧急）

反馈优先级采用四象限：

- `important_urgent`
- `important_not_urgent`
- `not_important_urgent`
- `not_important_not_urgent`

优先级由 Driver/任务语义产生，框架只根据优先级选择投递策略。

#### delivery_policy（投递策略）

投递策略不绑定渠道，但定义投递时机与队列语义：

- `immediate_insert`
  - 立刻插入一条新消息（打断式插入，不等下一轮）
- `after_current_turn`
  - 当前轮次结束后插入
- `throttle_insert`
  - 立即投递但带节流/合并
- `digest_schedule`
  - 汇总后定时投递（例如每日一次）

框架提供默认映射（可配置）：

- `important_urgent` -> `immediate_insert`
- `important_not_urgent` -> `after_current_turn`
- `not_important_urgent` -> `throttle_insert`
- `not_important_not_urgent` -> `digest_schedule`

Driver 可以覆盖默认投递策略。

## 3.6 Registry

Registry 是整个机制的注册中心。

Registry 负责管理：

- DriverPump 配置
- Event Package 注册
- Driver 注册
- Driver 与 Task 的绑定关系
- Driver 的激活索引和节奏配置

Registry 是稳定基础设施，不是业务逻辑层。

---

## 4. 系统交互链路

统一交互链路：

```text
DriverPump tick
-> 激活 Driver
-> Driver 自行采集 / 检查 / 构造 Event Package
-> Driver evaluation
-> Task 状态迁移
-> 若条件满足则创建 Run
-> Run 执行
-> Run 结果回写 Task
```

### 4.1 Driver 激活

Driver 可以由以下时机被激活：

- 固定轮询
- next_tick_at 到期
- 手动触发
- Run 回写后立即再评估
- 其他系统调度信号

框架不要求所有 Driver 以相同频率执行。

### 4.2 采集与检查

Driver 在激活时可以：

- 轮询外部系统
- 读取消息队列
- 检查缓存或数据库
- 读取内部状态
- 构造领域事件包

也就是说：

- 轮询本身就是采集
- 采集后的条件判断也就是事件检测

### 4.3 评估

所谓“触发”，本质上是 Driver 的一次 evaluation。

evaluation 可以表现为：

- 纯规则
- 用户定义代码
- 领域事件逻辑
- 历史状态辅助判断

### 4.4 状态迁移

Driver 决定 Task 状态如何迁移。

框架不写死统一状态机，但推荐 Task 至少支持：

- `active`
- `ready`
- `running`
- `blocked`
- `done`
- `failed`
- `cancelled`

具体状态集由 Task 类型和 Driver 决定。

### 4.5 Run 触发

当 Driver 判断条件满足时，Task 进入可执行态并创建 Run。

Run 可以立即执行，也可以交给外部执行器队列。

### 4.6 回写

Run 完成后，不直接结束流程，而是回写 Task：

- 更新状态
- 更新结果摘要
- 追加审计记录
- 触发新的后续驱动逻辑

---

## 5. 事件与驱动的边界

### 5.1 事件不是静态消息

事件不只是“收到了一条数据”。

事件可以是：

- 一段领域语义
- 一个被包装过的触发单元
- 一个带局部判定逻辑的事件对象

### 5.2 Driver evaluation 是条件判断

无论表现为：

- 轮询
- 事件匹配
- 条件检测

本质上都是：

- 在某个时机执行一组 if/else / 规则 / 用户定义逻辑
- 判断 Task 是否该进入下一状态

框架不试图消除这些判断，而是把它们明确放进 Driver。

### 5.3 Task 不定义事件语义

Task 绑定 Driver，但不解释事件本身。

Task 只保存：

- 目标
- 约束
- 状态
- 绑定关系

这样 Task 才能长期稳定。

### 5.4 Run 不决定状态机

Run 执行具体动作，但不拥有 Task 的业务状态机定义权。

Run 结果是输入。

Task 状态变更由 Driver 决定。

---

## 6. 股票买卖任务示例

示例目标：

- 股票价格高于上水线时卖出
- 股票价格低于下水线时买入

### 6.1 Task

```text
Task
- goal: AAPL 低于 176 买入，高于 182 卖出
- constraints:
  - symbol = AAPL
  - lower_line = 176
  - upper_line = 182
  - cooldown = 5m
  - max_qty = 100
- state = active
- driver_ref = stock.waterline.v1
```

### 6.2 Driver 输入来源

在当前主模型里，不要求有独立 Collector。

`stock.waterline.v1` Driver 在被激活时，可以自行：

- 轮询行情 HTTP
- 订阅/读取 WebSocket 缓存
- 读取券商价格推送缓存

这些都只是输入来源，不直接成为 Task 语义。

### 6.3 Event Package

不是把原始价格直接喂给 Task，而是包装成领域事件包。

例如：

```text
EventPackage
- event_name = quote.cross.waterline
- payload:
  - symbol = AAPL
  - current_price = 182.2
  - previous_price = 181.8
  - upper_line = 182
  - lower_line = 176
  - crossed_above = true
  - crossed_below = false
```

必要时该事件包还能带：

- `should_trigger(task)`
- `build_trigger(task)`

### 6.4 Driver

`stock.waterline.v1` Driver 可以定义：

- 激活频率：例如每 500ms 或 1s
- 自行获取最新价格与上一价格
- 如果 `crossed_above == true`
  - 生成 sell run
- 如果 `crossed_below == true`
  - 生成 buy run
- 如果 cooldown 未结束
  - 不触发

### 6.5 Run

当 Driver 命中后创建：

```text
Run
- action = sell
- symbol = AAPL
- qty = 100
```

执行完成后回写 Task：

- 更新时间
- 写入结果
- 进入 cooldown 或保持 active

这个例子说明：

- 原始输入不是 Task 事件本身
- Task 真正消费的是被包装后的领域事件
- 事件语义由用户定义，框架不预置

---

## 7. 工程约束

为了让这套机制可用于真实工程，而不是玩具系统，必须具备：

### 7.1 注册机制

- Event Package 可注册
- Driver 可注册
- DriverPump 策略可注册
- 注册关系可查询

### 7.2 版本机制

- 事件可版本化
- Driver 可版本化
- Task 可记录绑定版本

### 7.3 审计机制

- Driver 激活可追踪
- Driver 读取/构造的输入可追踪
- Driver 命中可追踪
- 状态迁移可追踪
- Run 触发与结果可追踪

### 7.4 重放机制

- 已记录事件可重放
- 重放时可重新驱动 Driver
- 可用于调试与回归验证

### 7.5 隔离机制

- 事件逻辑执行应有限制
- Driver 执行应有限制
- Run 执行应有限制
- 防止某个自定义事件或 Driver 把系统拖垮

### 7.6 沙箱与权限机制

如果事件允许包含可执行逻辑，则必须支持：

- 权限边界
- 运行超时
- 资源限制
- 错误隔离

---

## 8. 外部系统对照

这一节的目的不是照搬外部产品，而是明确：

- 当前行业里“Task”并不是一个统一概念
- 不同系统把它分别实现为：
  - 子代理委派工具
  - 计划/清单工具
  - 定时作业对象
  - 持久化运行时对象

因此，本文档必须先把这些语义拆开，再定义自己的协议。

### 8.1 Claude Code

Claude Code 当前存在两条容易混淆的线：

- 子代理委派
- 定时调度

在 Claude Agent SDK 中，子代理委派工具的官方名称已经是：

- `Agent`

并明确说明：

- `Agent` 之前叫 `Task`
- `Task` 目前仍作为别名被接受

这个概念的本质是：

- 主 Agent 创建一个子 Agent / subagent 去执行某个子任务
- 子 Agent 在隔离上下文中运行
- 子 Agent 返回摘要结果给主 Agent

因此这里的 `Task/Agent` 更接近：

- 一次性子任务委派工具
- 一个 spawn/delegate primitive

而不是：

- 持久存在的 Task 对象
- 可长期被驱动的任务状态机

Claude Code 的另一条线是 scheduled tasks：

- `/loop`
- reminder / 自然语言定时
- 底层 `CronCreate / CronList / CronDelete`

这部分才接近“计划任务”，但它默认是：

- session-scoped
- 用于重复执行 prompt

而不是统一的任务协议对象。

结论：

- Claude Code 中的 task-like 概念被拆成了：
  - `Agent/Task` = 子代理委派
  - scheduled task = 定时重跑 prompt

### 8.2 OpenCode

OpenCode 官方主概念是：

- `primary agents`
- `subagents`

其内建 subagent 包括：

- `General`
- `Explore`

这些 subagent 的作用是：

- 由主代理自动调用或手动 `@` 调用
- 在独立上下文中执行特定子任务
- 支持并行与上下文隔离

OpenCode 文档与社区实现里出现了 `Task tool` 这一说法，它本质上指向：

- 主代理把某个工作委派给 subagent

因此 OpenCode 中的 task-like 概念也更接近：

- 子任务委派
- subagent dispatch

而不是：

- 一个长期存在的 Task runtime object

OpenCode 还提供：

- `todowrite`

它的语义是：

- 维护复杂操作中的任务清单与进度

这更像：

- checklist
- progress tracker

而不是可驱动、可调度、可长期存在的 Task。

结论：

- OpenCode 有 task-like 概念，但主要语义是 subagent delegation
- 其 todo/task list 工具不等于持久任务对象

### 8.3 Hermes

Hermes 对相关概念的拆分最清楚，至少分为两类：

- `delegate_task`
- `cronjob`

`delegate_task` 的语义是：

- 生成一个或多个隔离子代理
- 子代理拥有自己的 conversation / terminal / toolset
- 只把最终摘要回给父代理

它适合：

- reasoning-heavy subtasks
- 并行研究
- 多文件分工

但它不是 durable 的。父流程中断时，子代理也会终止。

因此 `delegate_task` 更接近：

- 短生命周期子任务委派

而 `cronjob` 是 Hermes 明确提供的统一 scheduled-task manager，支持：

- `create`
- `list`
- `update`
- `pause`
- `resume`
- `run`
- `remove`

并且存在明确 CLI：

- `hermes cron create`

`cronjob` 的语义是：

- 一个被持久化的 job
- 可 one-shot，也可 recurring
- 可附带 skills
- 在 fresh session 中运行
- 可投递到不同渠道
- 有自己的存储与生命周期

结论：

- Hermes 显式地区分了：
  - `delegate_task` = 委派型子任务
  - `cronjob` = 持久化计划任务 / 作业对象

这比单纯把所有东西都叫 Task 更接近工程系统。

### 8.4 openGlow

在当前调研中，尚未找到能够稳定对应到同一官方产品的 `openGlow` 文档或仓库证据。

因此在本 spec 中暂不把它当作已确认参照系，只记录：

- 名称已出现于调研需求
- 当前缺少可验证的官方定义
- 后续如提供精确仓库/官网，再补正式对照

### 8.5 对本文档的启发

外部系统的共同问题是：

- “Task” 这个词被过度复用

它可能指：

- 一个子代理调用
- 一个 todo 项
- 一个 cron job
- 一个会跨轮次存在的状态对象

因此，本文档不直接沿用单一 `Task` 术语去吞掉所有含义，而是明确拆分为：

- `Task`
  - 薄状态对象，只表示目标、约束、状态与绑定关系
- `Run`
  - 一次具体执行
- `Driver`
  - 条件判断与状态推进协议
- `DriverPump`
  - 调度与激活机制
- `Executor`
  - 实际执行 Run 的主体

如果未来需要“像 Hermes cronjob 那样的持久作业对象”，也不应直接回退为模糊的 Task，而应明确它是：

- `Task + Driver + scheduling policy`

或者单独定义：

- `Job`

作为部署层对象，而不是污染 Task 的协议边界。

---

## 9. 独立部署与 NanoGhost 协作模型

本机制的推荐方向不是“继续长在 NanoGhost 内部”，而是：

- `Task System` 作为独立项目存在
- NanoGhost 通过协议接入该系统

这里的“独立”指的是：

- 独立代码库
- 独立进程/服务边界
- 独立存储与调度生命周期

而不是指：

- 必须一开始就物理分布式部署
- 不能与 NanoGhost 同机运行

也就是说：

- 协议与项目边界独立
- 部署方式可以灵活

### 9.1 为什么建议独立项目

如果 `Task` 直接寄生于 NanoGhost 内部实现，会自然污染上以下语义：

- 会话语义
- 渠道语义
- 当前轮次语义
- 当前 Agent 上下文语义

这样会导致：

- `Task` 难以跨渠道工作
- `Task` 难以交给非 Agent 执行器
- `Task` 难以成为长期存在的驱动对象
- `Task` 与当前产品形态强耦合

因此推荐把 `Task System` 作为独立项目抽离，持有：

- `Task Store`
- `Driver Registry`
- `DriverPump`
- `Run Store`
- `Executor Registry`
- `Feedback Router`

NanoGhost 不拥有 Task 本体，而只是接入它。

### 9.2 NanoGhost 在协作中的角色

当 `Task System` 独立后，NanoGhost 最自然的角色不是“任务系统本体”，而是以下几个角色之一或其组合：

- `Conversation Gateway`
  - 用户通过 NanoGhost 创建任务、查看任务、回复审批
- `Agent Executor`
  - 某些 Run 由 NanoGhost 的 Agent 执行
- `Feedback Surface`
  - Task System 将反馈、告警、摘要、审批请求投递给 NanoGhost
- `Human Approval Surface`
  - 用户在 NanoGhost 内对任务进行确认、拒绝、补充约束

因此两者关系不是：

- NanoGhost 内部有一套 Task

而是：

- NanoGhost 是 Task System 的一个接入方、一个执行器、一个反馈终端

### 9.3 控制面与执行面分离

推荐边界如下：

- `Task System` 负责控制面
- `NanoGhost` 负责部分执行面与交互面

`Task System` 负责：

- 任务创建与存储
- Driver 注册与调度
- 条件评估
- 状态迁移
- Run 创建
- 执行器路由
- 反馈投递策略

`NanoGhost` 负责：

- 接收用户输入
- 组织 Agent 上下文
- 执行一次 Agent Run
- 产出 RunResult
- 呈现反馈与审批交互

一句话：

- **Task System 决定“何时执行、执行给谁、结果如何回写”**
- **NanoGhost 决定“这一轮 Agent 如何完成被分配的 Run”**

### 9.4 推荐协作链路

推荐的标准链路如下：

```text
User / Scheduler / External Event
-> Task System
-> Driver evaluation
-> create Run
-> dispatch to NanoGhost or other Executor
-> executor returns RunResult
-> Task System writeback
-> Task System routes Feedback
-> NanoGhost / Feishu / Email / other sink
```

这个链路里：

- 长期存在的是 `Task`
- 短期执行的是 `Run`
- NanoGhost 只消费自己被分派的 `Run`

### 9.5 最小接口建议

为了让独立项目与 NanoGhost 协作，至少需要四类接口。

#### 9.5.1 CreateTask

方向：

- `NanoGhost -> Task System`

用途：

- 把一次对话中的“持续性要求”注册为 Task

示意：

```json
{
  "goal": "监控 PR #123 的 CI 状态，变绿后通知我",
  "constraints": {
    "repo": "org/repo",
    "pr": 123
  },
  "driver_ref": "github.pr-watch.v1",
  "owner_ref": "user.xxx",
  "source_ref": {
    "channel": "feishu",
    "conversation_id": "conv_xxx",
    "message_id": "msg_xxx"
  }
}
```

#### 9.5.2 DispatchRun

方向：

- `Task System -> NanoGhost`

用途：

- 把一个待执行 Run 投递给 NanoGhost Agent Executor

示意：

```json
{
  "run_id": "run_xxx",
  "task_id": "task_xxx",
  "executor_ref": "agent.nanoghost.main",
  "input_snapshot": {
    "goal": "检查 PR #123 当前是否可合并，并生成简短结论",
    "constraints": {
      "repo": "org/repo",
      "pr": 123
    }
  }
}
```

#### 9.5.3 ReportRunResult

方向：

- `NanoGhost -> Task System`

用途：

- 回传一次 Run 的执行结果

示意：

```json
{
  "run_id": "run_xxx",
  "ok": true,
  "summary": "CI 已通过，未发现阻塞项，建议进入合并确认",
  "artifacts": [],
  "metrics": {
    "duration_ms": 12000
  }
}
```

#### 9.5.4 DeliverFeedback

方向：

- `Task System -> NanoGhost`

用途：

- 把任务反馈、审批请求、状态通知投递回 NanoGhost

示意：

```json
{
  "task_id": "task_xxx",
  "run_id": "run_xxx",
  "sender": "task.github.pr-watch.v1",
  "recipients": ["conversation:feishu:abc"],
  "priority": "important_not_urgent",
  "delivery_policy": "after_current_turn",
  "content": {
    "type": "markdown",
    "text": "PR #123 的 CI 已通过，建议进入合并确认。"
  }
}
```

#### 9.5.5 Push 与 Pull 到底是什么

`push/pull` 指的是通信方式，不是任务语义。

在这个体系里，需要分清两类信息流：

- `Run` 分派：把“现在该执行什么”交给执行器
- `Feedback` 投递：把“发生了什么/要不要响应”交给会话与人

因此：

- `push run` 指的是：Task System 主动把 `DispatchRun` 送到某个 executor
- `pull run` 指的是：executor 主动来拉取/认领待执行的 run（而不是让 LLM 轮询）
- `push feedback` 指的是：Task System 主动把 `FeedbackMessage` 投递给 NanoGhost gateway / channel sink
- `pull feedback` 指的是：NanoGhost gateway 在固定时机拉取 inbox（而不是让 LLM 轮询）

“定时任务”并不依赖外部 push/pull：

- 定时只影响 `DriverPump tick` 与 Driver evaluation
- 当 tick 命中后，依然会落到 `create Run -> dispatch run -> writeback -> route feedback`

#### 9.5.6 详细执行方案：投递层的三段式模型

为了保证低成本、可审计、可控插入，推荐把投递层拆成三段：

```text
Task System
-> 生成 FeedbackMessage / RunRecord
-> 投递层（Delivery）
-> NanoGhost（inbox / conversation insert / digest）
```

其中投递层的核心目标是：

- 轮询发生在投递层，而不是 LLM 推理层
- 只把需要被感知的信息变成结构化消息投递出去

##### 9.5.6.1 Feedback 的落地存储：FeedbackInbox

Task System 在产生反馈时，不应直接依赖“立刻插入会话”。

推荐先落入一个可审计的 inbox：

- `FeedbackInbox`
  - key：`recipient_ref`
  - value：按时间排序的 `FeedbackMessage[]`

inbox 记录至少包含：

- message_id
- task_id / run_id（可选）
- priority / delivery_policy
- created_at
- status：`pending | delivered | acked | expired`

##### 9.5.6.2 Run 的落地存储：RunQueue（或 RunStore + Claim）

当 Driver 决定触发执行时，Task System 创建 Run。

Run 的存储与分派至少支持一种模式：

- push：Task System 直接调用 executor 的接收端点
- pull：executor 从 RunQueue 拉取并 claim
- queue：Run 写入消息队列，由 executor 消费

推荐 pull/claim 模式具备：

- `lease`（租约，防止多执行器重复领取）
- `idempotency`（幂等，防止重复执行产生副作用）

##### 9.5.6.3 投递到 NanoGhost 的三种通道

对 NanoGhost 来说，最终只需要三种落点：

- `conversation_insert`
  - 立刻插入会话（重要紧急）
- `agent_inbox_append`
  - 写入会话/agent inbox（重要不紧急）
- `digest_buffer_append`
  - 写入摘要缓冲池（不重要不紧急）

这三种落点是对外接口语义；底层传输方式可选择 push/pull/subscription。

#### 9.5.7 详细执行方案：什么时候触发“投递”

推荐把投递触发点固定在“状态变化与执行回写”上，而不是周期性把所有状态刷给 agent。

触发点最小集合：

- `Task state transition`
  - active -> ready / running / blocked / done / failed / cancelled
- `Run lifecycle`
  - started / finished(ok) / finished(error) / cancelled / timeout
- `Approval required`
  - 需要用户确认、补充约束、选择方案
- `Policy violation / quota`
  - 超预算、超频、权限不足、被拒绝
- `Driver health`
  - driver 连续失败、退避进入长间隔、数据源不可用

非触发点：

- 每一次 Driver tick 都投递
- 每一次价格查询都投递
- 每一次外部轮询都投递

这些会导致 agent 被噪音淹没，并且把轮询成本抬到 LLM 层。

#### 9.5.8 详细执行方案：push / pull / subscription 三种传输

##### 9.5.8.1 Push（webhook / queue）

push 适合：

- `important_urgent` 的反馈
- 审批请求
- 需要打断或尽快被看见的告警

典型形式：

- Task System -> NanoGhost gateway 的 webhook
- Task System -> MQ -> NanoGhost consumer

push 的要求：

- 必须幂等（message_id 去重）
- 必须可重试（至少 once 投递语义）
- 必须可降级（push 失败可回落到 inbox pull）

##### 9.5.8.2 Pull（inbox poll / run claim）

pull 适合：

- 普通优先级反馈
- 执行器取 run 的场景

pull 的关键点是：

- poll 发生在 gateway/worker，不发生在 LLM 推理层
- poll 有明确节奏与 backoff

建议两个 pull 接口：

- `PullFeedback(recipient_ref, cursor, limit)`
- `ClaimRuns(executor_ref, limit, lease_ms)`

##### 9.5.8.3 Subscription（长连接/流式）

subscription 是 push 的一种实现方式，语义仍然是 push。

例如：

- SSE / WebSocket / gRPC stream

它适合：

- 高频但需要低延迟的通知
- 但仍然必须遵守 delivery_policy，不允许直接把噪音灌进对话上下文

#### 9.5.9 详细执行方案：NanoGhost 什么时候把信息喂给 Agent

把“投递到 NanoGhost”与“进入 Agent 上下文”分开。

推荐加载时机：

- `immediate_insert`
  - gateway 直接插入会话流，形成一条系统消息
- `after_current_turn`
  - 当前 turn 完成后插入
- `throttle_insert`
  - 在一定窗口内合并后插入
- `digest_schedule`
  - 定时汇总后插入/发送

推荐读取时机：

- 每轮开始前，gateway 将 inbox 中少量高优先级反馈转为本轮输入前缀
- 用户显式查询时，agent 通过工具查询 task/run

禁止模式：

- 在没有新信息的情况下反复让 agent 调用工具“看看有没有更新”

### 9.6 NanoGhost 侧推荐适配层

为了避免重新把 Task System 塞回 NanoGhost 内核，NanoGhost 侧只建议增加薄适配层：

- `TaskClient`
  - 创建 Task
  - 查询 Task
  - 回复审批
- `TaskExecutorAdapter`
  - 接收外部 Run
  - 转成 NanoGhost 内部一次 Agent Run
  - 上传 RunResult
- `TaskFeedbackAdapter`
  - 接收 FeedbackMessage
  - 决定插入哪个会话、渠道或回执面

这个分层的好处是：

- NanoGhost 无需拥有 Task Runtime
- NanoGhost 只需要支持协议接入
- 未来替换 Task System 或新增其他执行器成本更低

### 9.7 与当前仓库的关系

本文档保留在当前仓库中，是为了先定义协议边界与未来映射关系。

但真正的 `Task System` 推荐：

- 单独起一个项目
- 单独维护存储、调度、驱动、执行器注册与反馈路由

当前仓库后续只需要实现：

- NanoGhost 作为 `Agent Executor`
- NanoGhost 作为 `Conversation Gateway`
- NanoGhost 作为 `Feedback Surface`

而不是在当前项目里继续生长完整 Task Runtime。

### 9.8 最终立场

最终立场可以压缩为一句话：

- **Task System 协议独立、项目独立；NanoGhost 通过接口接入，既可以是执行器，也可以是入口和反馈终端。**

---

## 10. NanoGhost 当前映射

本文档定义的是协议层。当前仓库中已有接近 Run 的机制，但尚未具备完整的 DriverPump / Driver / Task / Registry 分层。

### 10.1 已有近似 Run 的部分

- `Agent.chat_stream_events()`：当前主执行链
- `AgentExecutor.run()`（已内聚进 `agent.py`）：近似 Run 核心执行器
- `presenter.run_agent_turn()`：把一次消息驱动执行落到渠道输出

这部分已经很接近：

- `一次输入 -> 一次 Run -> 一个结果`

### 10.2 已有近似 Driver 输入来源的部分

- `src/agent_core/channel/feishu/sdk.py`
- `src/agent_core/channel/feishu/ws_client.py`
- `src/agent_core/channel/feishu/turn.py`

这些模块当前更像具体输入来源与渠道解析层，可被未来 Driver 直接消费或包一层 adapter。

### 10.3 尚未独立抽出的部分

- 可注册的 Event Package 体系
- DriverPump
- Driver Registry
- Task Store / Task Runtime Protocol
- Driver evaluation 协议

### 10.4 当前最接近的演进方向

不替换现有 Run，而是在其之上逐步补：

- `DriverPump`
- `Driver Registry`
- `Event Package Registry`
- `Task Store`

从而形成：

```text
DriverPump
-> Driver evaluation
-> Task
-> Run (existing Agent run path)
```

---

## 11. 非目标

本文档当前不定义：

- 统一任务 DSL
- 统一事件 DSL
- 固定状态机枚举全集
- 单一执行器实现
- 特定数据库模型
- 特定 UI / 管理台
- 特定渠道绑定策略

这些都可以在本协议层之上继续细化。

---

## 12. 一句话定义

### 框架定义

- **DriverPump 是 Driver 的调度与激活机制**
- **Event Package 是用户定义的可包装驱动单元**
- **Driver 是 Task 的驱动协议实现**
- **Task 是薄状态对象**
- **Run 是一次具体执行实例**

### 总模型

```text
DriverPump
-> Driver evaluation
-> Task State Transition
-> Run
-> Task Writeback
```

### 最终原则

- **框架负责机制**
- **用户负责语义**
