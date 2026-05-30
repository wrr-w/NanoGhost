# NanoGhost 操作记忆系统设计

> 版本: v1 — 2026-05-30
> 状态: 设计稿

---

## 一、设计目标

建立一个**分层操作记忆系统**，让 Agent 能参考历史操作模式来优化当前决策。

### 核心原则

1. **正交分层** — 语义（卡片）与结构（图）独立，不混用
2. **证据分级** — 明确告诉 LLM 每个建议的置信度来源
3. **按需钻取** — 默认零注入，LLM 通过工具主动查询
4. **由粗到细** — L1 最泛化最宽，逐层具体到原始命令

---

## 二、整体架构

```
用户输入
    │
    ▼
Agent 执行操作
    │
    ├──→ 操作记忆模块
    │       ├── 分类器（classifier） → 原始操作 → 4层编码
    │       ├── 图（graph）         → L1~L4 转移计数
    │       └── 卡片（card）        → L1 领域 + 语义 + 经验
    │
    ├──→ memory_check 工具（LLM 主动调用）
    │       ├── 卡片检索（embed→cosine）
    │       └── 图检索（from_code 匹配）
    │
    └──→ 决策循环
```

---

## 三、操作编码（OpCode）

每次操作（API 调用、Shell 命令）被编码为 4 层值：

```
L1 (8bit)     L2 (8bit)     L3 (32bit)         L4 (16bit)
 领域          动作          资源 hash          细节标志
  
0x04          0x03          0xA3B7C92D         0x0001
 CODE          RUN          [.py]              python
```

### L1 — 领域层（示例，非穷举）

> 分类器不维护此表。以下仅为便于理解的概念示例。
> 实际分类由 xxhash32(tool_name) 运行时自动分配。

| 概念 | 工具举例 | 说明 |
|------|---------|------|
| TERMINAL | terminal | shell 命令执行 |
| SKILL | skills_list, use_skill | SKILL.md 技能 |
| MCP | mcp__capture__* | Capture 等 MCP 工具 |
| DELEGATE | delegate_task | 子 agent 委派 |
| FILE_TOOL | read_file, write_file | 内置文件工具 |
| MEMORY | memory_write | 记忆操作 |
| ASK | ask_user | 用户交互 |
| 其他 | 任意新工具 | 自动分配新 code |

### L2 — 动作层（示例）

> L2 = hash(工具名)。以下仅为便于理解的概念示例。

| L1概念 | 工具 | L2含义 |
|--------|------|--------|
| TERMINAL | terminal → hash("terminal") | 所有 terminal 调用聚合 |
| MCP | mcp__capture__get_task_list → hash("mcp__capture__get_task_list") | 该工具所有调用聚合 |
| MCP | mcp__capture__create_task → hash("mcp__capture__create_task") | 不同工具有不同 L2 |

### L3 — 资源层（带参数特征）

L3 = hash(工具名 + 关键参数值)。
关键参数包括：id, task_id, query, keyword, name, path, url 等。
同一工具对不同资源调用，L3 不同。

### L4 — 细节层（保留扩展）

暂定 L4 = L3 + 时间戳窗口，用于区分同一参数的不同调用时机。
具体定义待实现时确定。

---

## 四、分类器（classifier）

分类器不做预定义分类表。它只做**特征提取**，用 hash 编码工具名和参数，
L1/L2/L3 在运行时自然涌现。

### 原则

1. **无需预定义** — 不需要注册 domain/action，不需要维护白名单
2. **可扩展** — 新工具自动获得编码，不需要改分类器代码
3. **语义自然涌现** — 同类工具 hash 相近，同类调用聚合到同一节点

### 分类算法

```python
def classify(tool_name: str, args: dict, server_id: str = "") -> OpCode:
    # MCP 工具: mcp__server_id__tool_name
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        # L1 = hash(协议 + 服务器)
        l1_source = parts[0] + "__" + parts[1]       # "mcp__capture"
        # L2 = hash(完整工具名)
        l2_source = tool_name                          # "mcp__capture__get_task_list"
        # L3 = hash(工具名 + 关键参数值)
        key_args = _extract_key_args(args)
        l3_source = tool_name + str(key_args)
        return OpCode(xxhash32(l1_source),
                      xxhash32(l2_source),
                      xxhash32(l3_source))

    # 内置工具
    if tool_name in _KNOWN_BUILTINS:
        return OpCode(xxhash32(tool_name),             # L1 = hash(工具类别)
                      xxhash32(tool_name),             # L2 = hash(工具名)
                      xxhash32(tool_name + str(args))) # L3 = 带参数

    # 未知工具 — 同样 hash，也能用
    return OpCode(xxhash32(tool_name),
                  xxhash32(tool_name),
                  xxhash32(tool_name + str(args)))


def _extract_key_args(args: dict) -> str:
    """提取关键参数值用于 L3 编码。跳过常量/标志类参数。"""
    key_fields = ["id", "task_id", "query", "keyword",
                  "name", "path", "url", "resource"]
    parts = []
    for k in key_fields:
        v = args.get(k)
        if v is not None:
            parts.append(f"{k}={v}")
    return "|".join(parts)
```

### 编码语义

| 层 | 来源 | 含义 |
|----|------|------|
| L1 | hash(domain) | 同类工具聚合。如所有 `mcp__capture__*` 工具共享一个 L1 |
| L2 | hash(tool_name) | 同工具聚合。如所有 `get_task_list` 调用共享一个 L2 |
| L3 | hash(tool_name + key_args) | 同参数调用聚合 |

第一次遇到新工具 → 自动分配 code → 写入图。
第二次遇到同样的工具 → 命中已有边，count+1。
不需要预定义、不需要改代码、不需要注册 meta。

---

## 五、存储模型

### memory.md — 双层存储

memory.md 全部由 Agent 自主写入，不做任何自动提取。两个 section：

| 层 | 内容 | 写入方式 |
|----|------|---------|
| daily_log | 每日完成事项、待办 | memory_write tool |
| decisions | 关键结论、设计决策 | memory_write tool |

原则：不做内容层自动提取（无正则、无关键词匹配）。Agent 通过提示词引导，在适当时机自主调用 memory_write 工具。

### 卡片表（卡片）

现有 `agent_memory_cards` 表，增加 `l1_code` 字段：

```sql
ALTER TABLE agent_memory_cards ADD COLUMN l1_code INTEGER DEFAULT 0;
ALTER TABLE agent_memory_cards ADD COLUMN l2_code INTEGER DEFAULT 0;
```

卡片记录：
- `intent_summary` — 用户意图（语义）
- `l1_code` — 该流程的主流领域
- `pitfalls` — 踩坑记录
- `experience_notes` — 经验总结
- 不再存完整步骤序列（由图接管）

### 图边表（操作转移）

新建 `agent_edges_ml` 替代旧 `agent_memory_edges`：

```sql
CREATE TABLE agent_edges_ml (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level INTEGER NOT NULL,           -- 1|2|3|4
    from_code INTEGER NOT NULL,
    to_code INTEGER NOT NULL,
    total_count INTEGER DEFAULT 0,
    approved_count INTEGER DEFAULT 0,
    namespace TEXT DEFAULT '',
    created_at REAL,
    updated_at REAL,
    UNIQUE(level, from_code, to_code, namespace)
);

CREATE INDEX idx_edges_ml_from ON agent_edges_ml(level, from_code, namespace);
CREATE INDEX idx_edges_ml_to ON agent_edges_ml(level, to_code, namespace);
```

### 操作索引表（raw → code 映射，可选）

```sql
CREATE TABLE agent_op_index (
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    l1_code INTEGER NOT NULL,
    l2_code INTEGER NOT NULL,
    l3_code INTEGER NOT NULL,
    l4_flags INTEGER DEFAULT 0,
    first_seen REAL,
    last_seen REAL,
    UNIQUE(method, path)
);
```

---

## 六、写入流程

每次 Agent 完成一轮工具调用后，后续步骤对写入：

```python
def record_operation_sequence(steps: list, intent: str, db, namespace):
    """记录一段操作序列到记忆系统。"""
    if not steps or len(steps) < 2:
        return

    # 1. 写入图（每对相邻步骤写 4 层）
    for i in range(len(steps) - 1):
        code_a = classify(steps[i])
        code_b = classify(steps[i + 1])
        for level in [1, 2, 3, 4]:
            upsert_edge(
                level=level,
                from_code=code_a.level_code(level),
                to_code=code_b.level_code(level),
                db=db, namespace=namespace,
            )

    # 2. 更新卡片 L1 统计
    l1_codes = [classify(s).l1 for s in steps]
    main_l1 = max(set(l1_codes), key=l1_codes.count)  # 主流领域
    update_card_l1(intent, main_l1, db, namespace)
```

---

## 七、检索流程

### memory_check 工具

```python
TOOL: memory_check
参数:
  query: str    — 语义查询关键词（可选，空则只查图）
  level: int    — 图查询层级 1|2|3|4，默认 2

描述:
  查看历史操作记忆。从卡片和经验开始，再到操作流统计。
  返回按置信度从高到低排列。

用法:
  - 不确定下一步做什么时调用
  - 上一步结果不理想时调用
  - level 默认 2（动作层），不够具体再深入
```

### 检索逻辑

```python
def memory_check(query: str, level: int, db, namespace, top_k=3):
    result = []

    # 1. 卡片检索（语义）
    if query:
        cards = retrieve_similar_flows(query, db=db, namespace=namespace, top_k=2)
        for card in cards:
            result.append({
                "type": "card",
                "confidence": "high",
                "intent": card.intent_summary,
                "l1": card.l1_code,
                "pitfalls": card.pitfalls,
                "experience": card.experience_notes,
            })

    # 2. 图检索（转移概率）
    if result:
        # 从卡片主流领域出发查图
        current_l1 = result[0].get("l1", 0)
        edges = query_edges(level, current_l1, db, namespace, top_k)
    else:
        # 没有卡片匹配，从当前操作系统环境判断
        current_code = classify_current_context()
        edges = query_edges(level, current_code, db, namespace, top_k)

    result.append({
        "type": "graph",
        "confidence": ["low", "medium", "high"][level - 1],
        f"L{level}_transitions": [
            {"to": format_code(e.to_code), "count": e.total_count}
            for e in edges
        ]
    })

    return result
```

### 输出格式

```
[经验] 相关操作: "启动服务"
  · 先检查端口 8092 是否被占用
  · 启动后用 /health 确认服务状态

[流向] 当前操作之后:
  L2 (动作层):
    → CODE:RUN (12次)
    → NETWORK:GET (8次)
    → FILE:READ (3次)
```

---

## 八、旧数据兼容

1. 旧 `agent_memory_edges` 表保留，但不再写入
2. 旧 `agent_memory_cards` 的 `steps_json` 字段保留不回填
3. 图迁移脚本：可选，从旧卡片步骤重建新图边

---

## 九、实现顺序

| 步骤 | 内容 | 文件 |
|------|------|------|
| 1 | OpCode 数据结构 | `memory/opcode.py` |
| 2 | 分类器（规则引擎） | `memory/classifier.py` |
| 3 | 多层图表建立 + 迁移 | `memory/graph.py` |
| 4 | memory_check 工具 | `memory/memory_check.py` + `tool/builtins.py` |
| 5 | 写入对接（从 agent post-process 调用） | `agent.py` → 调用 record_operation_sequence |
| 6 | 旧数据迁移脚本 | `scripts/migrate_memory.py` |
| 7 | 废弃旧 `agent_memory_edges` 写入 | `memory/graph.py` cleanup |
