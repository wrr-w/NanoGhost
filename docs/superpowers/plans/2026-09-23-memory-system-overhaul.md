# 记忆系统改造：实现计划（A + B + C）

> 2026-09-23 起草。**待确认后动手**。
> 目标：把「三库一模式」从设计变成**真的在跑**，并补上长期/当日分层与文本记忆新终态。
> 配套现状文档：`docs/memory-system-recap.md`。

---

## 0. 范围（三条线）

| 线 | 一句话 | 依赖新设计？ |
|---|---|---|
| **A** | Card / Graph 真的落数据（现在全是 0 行空库） | 否 |
| **B** | `memory.daily/` 真的写起来（长期/当日分层补完） | 否 |
| **C** | 文本记忆新终态「标签 × 日期、开放标签、内核零预设」 | **是（需确认，见 §5）** |

---

## 1. 根因（实测，不是猜）

### 1.1 写管线**从未执行**

`feishu.log` 里**只有检索侧**在打日志：

```
[AgentMemory] load_all_memory_cards took=0.001s, count=0
[AgentMemory] retrieve_similar_flows 耗时=0.0s, found=0
```

**完全没有** `Phase1 / Phase2 / Phase3 / Phase4` 任何一行 ——
而 `postprocess.py:30/37/39/41/54/61/74` 每个阶段都必然打日志。

→ **结论：`postprocess_turn` 一次都没被真正执行过。**

### 1.2 链路本身是接好的

```
agent.py:607  asyncio.create_task(agent.enqueue_memory_turn(...))   ← 调用点存在
agent.py:239  enqueue_memory_turn → pipeline.submit(event)
agent.py:233  _ensure_memory_pipeline() → MemoryPipeline(handler=_handle_memory_turn) + await start()
agent.py:225  _handle_memory_turn → postprocess_turn(...)
```

`MemoryPipeline` 是 **fire-and-forget 的常驻后台任务**（`pipeline.py:32 asyncio.create_task(self._run())`）。

**最可能的坑**：通道 worker 是**每请求/短生命周期的事件循环**，`create_task` 排的任务在回复发出后随循环一起没了；或 `_ensure_memory_pipeline` 的 `start()` 建在已关闭的 loop 上 → `_run` 直接死掉，`submit()` 只是往队列里堆没人取。

→ **A 的第 0 步就是把这条链跑到能出 Phase 日志为止。**

---

## 2. A —— Card / Graph 落数据

### A0. 定位并修好写管线（前置，必须先做）
- [ ] 在 `postprocess_turn` 入口 + `MemoryPipeline._run` 入口加打点，跑一轮，看事件到底卡在哪
- [ ] 依据结果二选一修：
  - 若是「任务被 loop 回收」→ 把 pipeline 生命周期**提升到进程级**（随网关启动而活），或用独立线程/`run_in_executor` 落库
  - 若是「start 建在错误 loop」→ 改为惰性绑定当前 running loop
- [ ] 验收：连续两轮对话后，日志出现 `Phase1 done: flow_hash=<非空>` 且 `agent_memory_cards` 行数 > 0

### A1. Card 落库打通
- [ ] `record_successful_flow` 全链走通（`_slim_steps` → `_flow_signature` → `flow_hash` → `save_memory_card` / 命中已有则 `success_count+1` + 意图样例 MMR 合并）
- [ ] 核对 `_prune_cards_by_tail_elimination`：`median*0.1` 阈值在**冷启动（少量卡）**时会不会把仅有的卡也删掉 → 冷启动保护
- [ ] 验收：同一条 flow 跑两次 → `success_count=2`，不是两行

### A2. Graph 落库打通
- [ ] `update_graph_ml`（`graph.py:11`）：相邻步 → `for level in [1,2,3,4]` → 写 `agent_edges_ml`；核对 `classify()` 是否对 MCP 工具名（`capture_*` / `ticketcore_*`）返回了有效 `level_code`
- [ ] `if method_a==method_b and path_a==path_b: continue` 对**纯工具名不同、path 为空**的步（MCP 调用）会不会被误判成同一步 → 修判重 key
- [ ] 验收：跑一轮多步对话后 `agent_edges_ml` 行数 > 0

### A3. 检索侧
- [ ] Card 有数据后，`retrieve_similar_flows` 应能命中（现恒 0）；核对 `MEMORY_MIN_SIM=0.4`
- [ ] 注入不变（仍然**只注入索引**，正文靠 `memory_read` 钻取）

### A4. 单元测试（新增/扩展 `tests/test_memory_cards.py`）
- [ ] `_flow_signature` / `flow_hash` 稳定性（同步骤 → 同 hash）
- [ ] 首次写入 / 二次命中（`success_count` 递增、`intent_examples` 去重 + MMR 截断）
- [ ] 全失败步骤 → 不落卡
- [ ] 冷启动不被剪枝（1~2 张卡时 `_prune` 不删）
- [ ] `update_graph_ml`：3 步 → 2 组边 × 4 level；同 method+path 相邻步不产出边
- [ ] `classify()` 对 MCP 工具名的 level_code 快照

---

## 3. B —— memory.daily 跑起来

现状：`files.py` 已有 long/daily 路径函数；`presenter.py` 有当日块注入；**但 `memory.daily/` 目录从没被创建**。

### B1. 写入侧
- [ ] 明确「当日记忆」的写入口：
  - ① 回合末由 `postprocess` 写一条流水（与 A0 的管线修复共用）
  - ② `memory_write` 工具：**当日类**条目（临时/进行中）路由到 `memory.daily/YYYY-MM-DD.md`，**长期类**写 `memory.md`
- [ ] `files.py` 加 `ensure_daily_dir()` + 首次写入原子创建
- [ ] 验收：一次对话后 `memory.daily/2026-09-23.md` 出现且有内容

### B2. 读取/注入侧
- [ ] 核对 `presenter.py` 每轮注入当日块（**只注入当日**，不碰长期）
- [ ] 复核长期=索引 / 当日=全文的边界，避免两边重复

### B3. 单元测试（扩展 `tests/test_memory_files.py`）
- [ ] long/daily 路径解析（跨平台分隔符、日期格式）
- [ ] 当日写入自建目录；跨日 → 新文件
- [ ] 注入只覆盖当日、不回灌长期

---

## 4. C —— 文本记忆新终态（**设计方案，待你确认**）

> 仓库里**没有**任何 spec，只有上个会话留下的三个关键词。以下是我给出的落地方案，**请你点头或改**。

### C0. 三个关键词的解读

| 关键词 | 我的解读 |
|---|---|
| **内核零预设** | 内核**不预置任何标签体系/分类树**；不写死"工作/生活/项目"这类固定类目 |
| **开放标签** | 标签**从内容里生长**（写入时生成，读取时聚合），可新增、可合并、可消亡；内核只做**存储与索引**，不判优劣 |
| **标签 × 日期** | 记忆按 **(标签, 日期)** 双键组织：日期给"何时"，标签给"何事"；读取天然支持「某标签的时间线」与「某天发生了什么」两种切法 |

### C1. 数据模型
```
memory.md                      ← 长期（人工/内核沉淀）
memory.daily/YYYY-MM-DD.md     ← 当日流水
memory.tags.json               ← 开放标签表：{tag: {first_seen, last_seen, count, entry_ids[]}}
```
- 每条记忆 = `{id, date, tags:[开放], text, source(turn|tool|manual)}`
- **标签零预设**：写入时由 LLM/规则从内容抽 0~N 个开放标签（抽不出就只挂日期）
- **内核不判好坏**：只记录、只计数、只提供"共现/时序"查询

### C2. 写入
- [ ] `memory_write` 支持 `tags=[...]`（可空）；空则由 LLM 抽取（**只生成、不判断**，遵守 flow-rules）
- [ ] 自动更新 `memory.tags.json`（首次出现/最后出现/计数）

### C3. 读取 / 注入
- [ ] 注入：**标签索引**（Top-N 标签 + 计数 + 最近日期）+ 当日块；正文按需 `memory_read`
- [ ] 查询：`memory_read(section=tag|date, ...)`
- [ ] 保持 3 层披露（Index → Locate → Detail）不变

### C4. 迁移
- [ ] 现有 `memory.md` 单节 `project_context` **不强行拆标签**（保留人工结构），只在新写入侧启用「标签×日期」
- [ ] 提供一次性脚本：为可选老条目补标签（**人工确认后才写**）

### C5. 单元测试（新增 `tests/test_memory_tags.py`）
- [ ] 标签抽取：0 标签 / 1 标签 / 多标签 三种输入
- [ ] `memory.tags.json` 增删与计数正确、幂等
- [ ] (标签, 日期) 双键查询；跨日合并
- [ ] 注入只给索引、不泄漏正文

---

## 5. 任务分解与顺序（建议）

```
A0 修写管线（前置，卡点）
      │
      ├─ A1 Card 落库 ─┐
      ├─ A2 Graph 落库 ─┤─ A4 单测
      └─ A3 检索命中 ──┘
B1 当日写入 ─ B2 注入核对 ─ B3 单测
C1 模型 ─ C2 写入 ─ C3 读取 ─ C5 单测 ─ C4 迁移（最后）
```

**依赖**：B、C 的"回合末写入"都复用 **A0 修好的管线** → **A0 必须最先**。

---

## 6. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 改管线生命周期影响主回复链路 | 后处理**永远不阻塞回复**；失败只记日志（现状已如此）|
| 冷启动误剪枝把卡删空 | A1 加保护 + 单测 |
| C 的标签抽取引入 LLM 调用变慢 | 复用每 flow 那**唯一一次** LLM；抽不出就空标签 |
| 改 DB 结构 | 只**加表/加列**，不动既有列；迁移脚本先 dry-run |

**回滚**：全部改动集中在 `src/agent_core/memory/*` + `tool/builtins/memory.py` + `presenter.py`，单 commit 可 revert。

---

## 7. 交付物

- 代码：`memory/{pipeline,postprocess,cards,graph,files,intent}.py`、`tool/builtins/memory.py`、`presenter.py`
- 单测：`tests/test_memory_cards.py`（新）、`tests/test_memory_tags.py`（新）、`tests/test_memory_files.py`（扩）、`tests/test_memory_pipeline.py`（扩）
- 验收：日志出现 Phase1~4 + 两表行数 > 0 + `memory.daily/` 有文件
