# NanoGhost 操作记忆系统设计

> 版本: v2 - 2026-05-30
> 状态: 设计稿

---

## 一、设计目标

建立一个分层操作记忆系统，让 Agent 能通过历史模式辅助当前决策。

### 核心原则

1. **正交分层** - Card（语义）与 Graph（结构）使用同一数据源的不同视图
2. **按需钻取** - 默认零注入，LLM 通过工具主动查询图
3. **LLM 自主决策** - Graph 只摆选项，不排序不推荐，LLM 用意图选路
4. **由粗到细** - L1 最泛化最宽，逐层具体到原始命令

---

## 二、整体架构

```
用户输入
    |
    v
Agent 执行操作
    |
    +--> 回合结束（有 tool calls）
    |       +-- Card 写入（record_successful_flow）
    |       |   +-- flow_hash + intent + steps + LLM经验总结
    |       |
    |       +-- Graph 写入（update_graph_from_steps）
    |       |   +-- 从 Card.steps 切相邻对 -> L1~L4 边计数
    |       |
    |       +-- memory.md（Agent 自主 memory_write）
    |
    +--> 检索时（LLM 主动调用）
    |       +-- Card 检索（语义 -> 完整路径参考）
    |       +-- Graph 查询（当前节点 -> 出边选项）
    |
    +--> LLM 综合决策
            +-- Card: "上次做类似事走了 A->B->C->D"
            +-- Graph: "当前节点有三条出边 -> X, Y, Z"
            +-- 意图 + 上下文 -> 选一条
```

### 两种存储，同一数据源

```
Card（纵切）                          Graph（横切）
---------------------                ---------------------
意图 -> A -> B -> C -> D -> 完成      A->B count: 12
                                     A->C count: 8
flow_hash: xxx                       B->D count: 5
steps: [A, B, C, D]                  C->E count: 3
experience: "注意xxx..."
```

**Card 是完整路径记录** -- 存意图、步骤序列、经验总结。
**Graph 是步骤转移统计** -- 从所有 Card 的步骤序列中，切出相邻对，累计每个转移出现次数。

它们不是两套独立数据：Graph 的边是从 Card 的步骤序列中切出来的。Card 变动时 Graph 不可能独立变化。

---

## 三、统一原则：分层披露

### 为什么需要分层披露？

随着记忆积累，Graph 边可以上万条，Card 可以成百上千个，memory.md 可以上百行。
如果每次查询都返回完整内容，信息量会大到 LLM 无法消化。

所以三种存储都必须遵循同一个查询路径：**先看概览 → 定位到局域 → 再深入细节**。

```
第一层：索引/分类
  让我看看"当前有哪些可用的记忆" → 返回索引级概览

第二层：定位到领域
  聚焦到某个分类 → 返回该领域的摘要级内容

第三层：展开细节
  深入某条具体记录 → 返回完整详情
```

### 三种存储如何各自实现

| 存储 | 第一层（索引） | 第二层（定位） | 第三层（细节） |
|------|--------------|--------------|--------------|
| **Graph** | L1 领域总览 | L2 动作总览 | L3 资源/ L4 细节 |
| **Card** | 按 namespace / L1 分类列表 | 语义检索 + 领域过滤 | 完整 flow 详情 |
| **memory.md** | 看 section 列表 | 看某个 section 内容 | 展开某条记录全文 |

### Graph 的分层

- `memory_explore("node")` 返回 L1+L2 → 定位到方向
- `memory_explore("drill")` 返回 L3+L4 → 展开细节
- Graph 的数据天然分层，设计与实现已有

### Card 的分层

当前 `retrieve_similar_flows()` 是 embedding 一路到底，没有先分类再搜索。

未来的分层设计：
- L1 分类查询：`list_cards(domain="IWMS")` → 返回该领域的卡片摘要列表
- 语义检索（已有）：`retrieve_similar_flows("创建任务")` → 在当前领域内搜索
- 详情展开（已有）：点击某卡片 → 返回完整 steps + experience

### memory.md 的分层

当前 memory.md 全文注入到每轮 system prompt，数据量大时不可持续。

未来的分层设计：
- 顶层：`memory_read(section="all")` → 返回 section 索引（标题列表 + 行数）
- 中层：`memory_read(section="daily_log")` → 返回该 section 内容
- 底层：全文注入仅保留最近 N 行，完整内容通过 tool 按需获取

注入策略调整：memory.md 不再全文注入，改为只注入 section 索引。LLM 需要时通过 `memory_read` tool 获取具体 section 内容。

---

## 四、Card -- 路径参考（语义+经验）

### 数据模型

```python
@dataclass
class AgentMemoryCard:
    id: str
    flow_hash: str              # 步骤序列的 SHA256
    intent_summary: str         # 用户意图（语义检索用）
    intent_vector: List[float]  # Embedding
    steps: List[Step]           # 步骤序列（给 Graph 喂数据）
    success_count: int
    total_rounds: int
    experience_notes: List[str] # 经验总结（LLM 每次 flow 完成后生成）
    # 不再有 pitfalls 字段
```

### 写入流程（回合结束）

```
有 tool calls 且有回复
    |
    +-- record_successful_flow()
    |   +-- 生成 flow_hash（步骤序列 hash）
    |   +-- 写入 intent_summary + steps + embedding
    |   +-- 更新 success_count（已有同 hash 则 count+1）
    |
    +-- LLM 总结经验（每次都调）
    |   +-- 输入: intent + steps + reply
    |   +-- 输出: 一段经验文本，或空（没什么好记的）
    |   +-- 去重 -> card.experience_notes
    |
    +-- 保存 DB
```

**变化**：
- 不再有 pitfalls 字段（步骤级失败检测 + LLM 调用）
- 不再有 "累计 5 次才总结" 阈值
- 每次 flow 完成都调 LLM
- LLM 自己判断这次值不值得记

### 检索流程

给定用户意图 -> get_embedding -> cosine 相似度 -> MMR 多样性重排 -> 返回 top_k 相似 Card

每个 Card 附带：
- intent_summary -- 意图描述（LLM 判断是否相关）
- steps -- 完整步骤序列（LLM 参考路径）
- experience_notes -- 经验总结（LLM 参考注意事项）

---

## 五、Graph -- 选项地图（结构+统计）

### 角色定义

Graph 不是推荐系统。它的职责只有一条：

> **从当前节点出发，历史上去过哪些其他节点？**

不做：
- 评分/排序/推荐哪条边更好
- 计算转移概率
- 过滤不重要的边

只做：
- 列出所有出边及其原始计数
- LLM 自己用意图 + 上下文判断选哪条（或全都不选）

### 数据模型

```sql
CREATE TABLE agent_edges_ml (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level INTEGER NOT NULL,
    from_code INTEGER NOT NULL,
    to_code INTEGER NOT NULL,
    total_count INTEGER DEFAULT 0,
    namespace TEXT DEFAULT '',
    created_at REAL,
    updated_at REAL,
    UNIQUE(level, from_code, to_code, namespace)
);
```

**变化**（相对于当前 graph.py）：
- 移除 approved_count、approved_ratio
- 移除 relation_type（不再区分 FOLLOWS/DEPENDS_ON）
- 移除 _score() 排序
- 移除 _prune_edges() 剪枝（保留所有边，由 LLM 决定哪些有用）
- 加 level 字段，支持多层查询

### 多层分层（L1~L4）

详见 memory-multilayer-spec.md。

| 层 | 粒度 | 示例 | 用途 |
|----|------|------|------|
| L1 | 领域 | FILE -> API | 最宽，看大方向 |
| L2 | 动作 | GET -> POST | 看操作类型变化 |
| L3 | 资源 | /api/tasks -> /api/tasks/{id} | 看资源流转 |
| L4 | 细节 | 含参数区别 | 最精确 |

### 查询接口

```python
def query_outgoing_edges(
    current_node: int,
    level: int = 2,
    top_k: int = 10,
    db = ...,
    namespace = ...,
) -> List[dict]:
    edges = db.query(
        "SELECT to_code, total_count FROM agent_edges_ml "
        "WHERE level=? AND from_code=? AND namespace=?",
        level, current_node, namespace
    )
    return sorted(edges, key=lambda e: e.total_count, reverse=True)[:top_k]
```

### 写入规则

```python
def record_step_pair(step_a, step_b, db, namespace):
    code_a = classify(step_a)
    code_b = classify(step_b)
    for level in [1, 2, 3, 4]:
        from_val = level_code(code_a, level)
        to_val = level_code(code_b, level)
        upsert_edge(level, from_val, to_val, db, namespace)
```

---

## 六、Card + Graph 联动

### 写入时联动（自动）

```
回合结束，有 steps
    |
    +-- record_successful_flow()
    |   +-- Card 写入成功
    |   +-- 从 steps 中切出相邻对 -> 传给 update_graph_from_steps()
    |
    +-- update_graph_from_steps()
        +-- 遍历步骤对 (i, i+1)
        |   +-- 用 classify() 编码每个 step
        |   +-- 写 L1~L4 四层
        +-- 保存到 agent_edges_ml
```

Card 的 steps 是 Graph 的唯一数据来源。没有 Card 就没有 Graph。

### 检索时联动（LLM 主动）

```
LLM 不确定下一步做什么
    |
    +-- 第一步：查 Card（语义检索）
    |     返回: 相似意图的完整路径 + 经验
    |     例如: "上次做类似事走了 A->B->C->D"
    |
    +-- 第二步：查 Graph（当前节点出边）
    |     当前节点: E
    |     Graph 返回: E->F(5次), E->G(3次), E->H(1次)
    |
    +-- LLM 综合判断
         "意图是 X。Card 说上次走 A->B->C->D。
          但当前我在 E 节点，Graph 说从 E 可以到 F, G, H。
          其中 G 符合我的意图，且与 Card 中 C->D 的上下文一致，
          所以选 G。"
```

联动不在于代码，在于 LLM 同时看到了两种信息之后自己做推理。

---

## 七、内置 Tool：memory_explore（查询 Graph）

### 设计原则

- Graph 不做任何自动注入（LLM 通过 tool 按需获取）
- 工具是统一入口，两个 action 控制粒度
- LLM 自己决定什么时候查、查哪层

### 接口

```
memory_explore(action: str, args: dict)

动作1: "node" — 查看当前节点的出边
  输入:
    method: str    — 当前步骤的 method
    path: str      — 当前步骤的 path
    namespace: str — 命名空间（可选）

  返回:
    L1 (领域层): 当前节点的领域级出边
      [domain] -> [domain] (count)
    L2 (动作层): 当前节点的动作级出边
      [method] -> [method] (count)

  数据量: L1 + L2 通常不超过 10 条，够 LLM 决定方向。

动作2: "drill" — 展开某条边到资源层
  输入:
    from_method: str
    from_path: str
    to_method: str
    to_path: str
    level: int — 默认 3（资源层），可选 4（细节层）

  返回:
    L3 (资源层): 该边对应的具体资源流转
      [path] -> [path] (count)
    L4 (细节层): 含参数细节

  数据量: L3 可能 5-10 条，LLM 据此选具体路径。
```

### 使用示例

```
LLM 不确定下一步做什么
  |
  +-- memory_explore(action="node", method="GET", path="/api/tasks")
  |
  返回:
    [L1] API -> API (8次)
    [L2] GET -> POST (6次), GET -> GET (3次), GET -> PUT (2次)
  |
  +-- LLM:"GET -> POST 是最常见的，展开看看具体到哪些资源"
  |
  +-- memory_explore(action="drill",
                      from_method="GET", from_path="/api/tasks",
                      to_method="POST", to_path="/api/tasks")
  |
  返回:
    GET /api/tasks -> POST /api/tasks (4次)
    GET /api/tasks -> POST /api/reports (2次)
    GET /api/tasks -> POST /api/settings (1次)
  |
  +-- LLM:"当前意图是创建新任务，选 POST /api/tasks"
```

### 和 Card 检索的配合

```
LLM 查看历史记忆的两种方式：
  - memory_explore() -> 查 Graph，看结构流向
  - retrieve_similar_flows() -> 查 Card，看语义相似的完整路径（已有）

两者独立使用，LLM 按需调用：
  - 不确定怎么做 -> memory_explore("node") 看当前选项
  - 想看类似场景 -> retrieve_similar_flows() 查语义相似
  - 想深入某条边 -> memory_explore("drill") 展开细节
```

---

## 八、memory.md

### 写入

全部由 Agent 通过 memory_write tool 自主写入。

两个 section：
- daily_log -- 每日完成事项
- decisions -- 关键结论、设计决策

不做任何内容层自动提取。无正则、无关键词匹配。

### 读取（分层披露）

当前：全文注入 system prompt，数据量大时不可持续。

未来改造为分层读取：

```
第一层：memory_read(action="index")
  返回 section 标题列表 + 每个 section 的行数/记录数
  → "有两个 section：daily_log（12行），decisions（8行）"

第二层：memory_read(action="section", name="daily_log")
  返回该 section 的完整内容
  → "2026-05-30: 修复 YAML 解析器..."
  
第三层：memory_read(action="detail", section="decisions", keyword="分层")
  返回匹配该关键词的具体条目
  → "分类器不依赖生态工具自声明标注"
```

注入策略调整为：
- 不再全文注入。每轮只注入 section 索引（标题 + 行数）
- LLM 需要时通过 memory_read tool 获取具体内容
- 或者：保留最近 N 行自动注入，完整内容走 tool

---

## 九、实现顺序

| 阶段 | 内容 | 涉及文件 |
|------|------|---------|
| 1 | Graph 去评分、去剪枝、去 approved_count | graph.py |
| 2 | Card 去 pitfall、改 experience 触发 | cards.py |
| 3 | Graph 多层：classify() + OpCode + 分层写入 | classifier.py 新建 + graph.py 改造 |
| 4 | 内置 Tool：memory_explore（node + drill） | tool/builtins.py |
| 5 | 内置 Tool：memory_read（index + section + detail） | tool/builtins.py |
| 6 | Card 分层索引：list_cards(domain) | cards.py |
| 7 | 清理旧数据、迁移脚本 | utils/migration.py |

阶段 1+2 是纯删减，可立即做。阶段 3~6 是新增，按需推进。
