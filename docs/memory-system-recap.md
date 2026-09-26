# 记忆系统：现状 recap + 四版设计思路

> 整理于 2026-09-23。**现状部分全部是实测**（库查询 + 日志计数 + 代码走查），非文档转述。
> 面向「记忆系统改造」任务：先对齐现状与历史思路，再选线落地。

---

## 0. TL;DR

- 设计上是**三库一模式**（Card / Graph / memory.md，统一「分层披露」）：**实际只有 memory.md 活着**。
- **Card、Graph 都是 0 行的空库**；写入流水线挂着但基本空转（Phase4 = 0 次、Graph 存盘 = 0 次）。
- **长期 / 当日分层只做了一半**：注入侧有，实例里**根本没有 `memory.daily/` 目录**。
- 历史上有**四版思路**：v2 分层操作记忆 → v3「三库一模式」（Active）→ flow-rules（把 v3 写成流程节点规则）→ Memory/MCP Layering（长期/当日分离 + MCP 感知分层）。
- 记忆里登记的**未决**：图记忆只读不写；文本记忆终态定为「**标签 × 日期、开放标签、内核零预设**」（待落地）。

---

## 1. 现状（实测）

### 1.1 三库 vs 实际数据

| 存储 | 落点 | 行数 | 状态 |
|---|---|---:|---|
| **Card**（流程卡：意图→步骤→经验） | `agent_memory_cards` | **0** | 空库 |
| **Graph**（步骤转移计数图） | `agent_edges_ml` | **0** | 空库 |
| **memory.md** | `instances/cc/memory.md` | 134KB / 单节 `project_context` | ✅ 唯一活的，全靠人工 `memory_write` |

> 另有 `agent_messages` 3511 行（会话历史，不是记忆系统本身）。

### 1.2 写入流水线（挂是挂了，没产出）

调用点：`agent.py` 回话后 `enqueue_memory_turn(...)` → `memory/postprocess.py:postprocess_turn()`，四阶段：

```
Phase1  Card 写入 (record_successful_flow)
Phase2  Graph 写入 (update_graph_ml)
Phase3  LLM 生成经验（每 flow 一次）
Phase4  memory.md 汇总 (summarize_to_memory_md)
```

日志实测（`nanoghost.log` / `feishu.log`）：

| 标记 | 出现次数 |
|---|---:|
| `Phase1` | 6 |
| `Phase2` / `Phase3` | 2 / 2 |
| `Phase4` | **0** |
| `[Graph] saved … edges` | **0** |

→ 流水线**跑过几次但产出为零**：cards=0、edges=0、Phase4 从未执行。

### 1.3 检索侧（每轮都在打，永远落空）

```
load_all_memory_cards  took=0.001s, count=0
retrieve_similar_flows 耗时=0.0s, found=0
```

→ **语义检索形同虚设**（库是空的）。

### 1.4 长期 / 当日分层

- `memory/files.py`（182 行）：长期=`memory.md`，当日=`memory.daily/YYYY-MM-DD.md`
- 注入：`run.py` 注入长期**索引**；`presenter.py` 每轮注入当日块
- 实测：`memory.daily/` **目录不存在** → 当日记忆这条线**从未写入**

### 1.5 注入策略

- `memory_inject=index`：只给章节索引 + 内联「短章节」，长章节正文按需 `memory_read` 钻取
- 提示词分 6 层：Identity / Memory / **Capability Awareness**(MCP 清单) / Runtime Readiness / …

### 1.6 模块清单

| 文件 | 行数 | 职责 |
|---|---:|---|
| `memory/cards.py` | 479 | Card 读写 / 语义检索 / 尾部淘汰 |
| `memory/files.py` | 182 | 长期 vs 当日文件路径 |
| `memory/intent.py` | 180 | 意图摘要 / 往 memory.md 汇总 |
| `memory/embedding.py` | 72 | 向量 |
| `memory/pipeline.py` | 69 | 四阶段串联 |
| `memory/postprocess.py` | 80 | 回合收尾编排 |
| `memory/classifier.py` | 69 | L1 领域分类 |
| `memory/graph.py` | 58 | Graph 读写 |
| `tool/builtins/memory.py` | 330 | memory_write/read（含当日路由） |

---

## 2. 四版设计思路

### ① v2 设计 —— `docs/memory-design-v2.md`（2026-05-30）

- 分层**操作记忆**：Card 竖切（意图→步骤→经验）+ Graph 横切（步骤转移计数），**同一数据源两个视图**
- 默认零注入，**LLM 主动钻取**
- Graph 只摆选项、**不排序**；由粗到细
- 取消「pitfall」概念（并入经验）

### ② v3 spec —— `docs/memory-system-v3-spec.md`（2026-05-30，标注 **Active**）

**三库一模式 = 统一的三层披露（Index → Locate → Detail）**：

| | 一层 | 二层 | 三层 |
|---|---|---|---|
| Card | 按 namespace/L1 列表 | 语义检索 + 领域过滤 | 完整 flow |
| Graph | L1 领域总览 | L2 动作总览 | L3 资源 / L4 细节 |
| memory.md | section 索引 | section 内容 | 单条全文 |

关键决定：
- 取消 pitfall；Graph **不做评分推荐**
- memory.md **不做内容自动抽取**
- **每个 flow 只调 1 次 LLM** 生成经验
- 一切查询走工具；**只注入 memory.md 的索引**

### ③ flow-rules spec —— `docs/memory-flow-rules-spec.md`

把 ② 落成「流程节点」规则：
- **LLM 只在「生成内容」时介入，不参与判断**
- 明确 LLM **不做的 6 件事**（不判该不该记 / 不判失败 / 不判重试 / 不排序推荐 …）
- Phase0 入口条件 = **有工具调用 + 有回复**

### ④ Memory / MCP Layering —— `docs/superpowers/{plans,specs}/2026-06-09-memory-mcp-layering*.md`（最新）

- 提示词拆 **6 层**：Identity / Memory / Capability Awareness / Runtime Readiness / …
- **长期记忆 vs 当日工作记忆彻底分离**（新增 `memory/files.py` + `memory.daily/`）
- **MCP 感知与执行就绪分离**：模型要知道「有哪些 MCP、ready 没 ready」，但**只调已注册 schema 的工具** → 即当前注入的 `## MCP 能力概览`

---

## 3. 设计 vs 实现的鸿沟（改造靶子）

1. **Card / Graph 从没落过数据** —— 写路径挂着、库空的、检索每轮空转
2. **`memory.daily/` 从没建过** —— 长期/当日分层只做了注入侧
3. **memory.md 独木撑** —— 134KB 单节，人工维护，长期/当日不分
4. retrieval 每轮空跑，白耗时间

## 4. 未决（记忆里登记的）

- 图记忆仍是空库（**只读不写**）
- 文本记忆终态已定稿为「**标签 × 日期、开放标签、内核零预设**」（待落地）

## 5. 下一步候选（三选一）

- **A** Card / Graph 落数据（让 ① 的竖切+横切真正跑起来）
- **B** `memory.daily/` 跑起来（补完 ④ 的长期/当日分离）
- **C** 落地文本记忆新终态「标签 × 日期、开放标签、内核零预设」
