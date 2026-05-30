# 多层操作记忆图设计

## 一、数据模型

### OpCode — 操作编码

每个操作步骤用一个 OpCode 表示，分 4 层。

```
@dataclass
class OpCode:
    raw_method: str    # "EXEC" | "GET" | "POST" | etc
    raw_path: str      # 原始命令或API路径
    l1: int            # 领域 (8bit)
    l2: int            # 动作 (8bit)
    l3: int            # 资源hash (32bit)
    l4: int            # 细节标志 (16bit)
    reserved: int      # 保留 (64bit)
    
    @property
    def level_code(self, level: int) -> int:
        \"\"\"返回指定层的掩码值\"\"\"
        masks = {1: self.l1,
                 2: (self.l1 << 8) | self.l2,
                 3: (self.l1 << 40) | (self.l2 << 32) | self.l3,
                 4: (self.l1 << 72) | (self.l2 << 64) | (self.l3 << 32) | self.l4}
        return masks.get(level, self.full)
    
    @property
    def full(self) -> int:
        return (self.l1 << 120 | self.l2 << 112 | 
                self.l3 << 80 | self.l4 << 64 | self.reserved)
```

## 二、存储

### 表结构

```sql
-- 多层图边
CREATE TABLE agent_edges_ml (
    level INTEGER NOT NULL,        -- 1|2|3|4
    from_code INTEGER NOT NULL,    -- 该层的编码值
    to_code INTEGER NOT NULL,
    total_count INTEGER DEFAULT 0,
    approved_count INTEGER DEFAULT 0,
    namespace TEXT DEFAULT '',
    created_at REAL,
    updated_at REAL,
    UNIQUE(level, from_code, to_code, namespace)
);

CREATE INDEX idx_edges_ml_lookup ON agent_edges_ml(level, from_code, namespace);
```

### 写入

```python
def record_step_pair(step_a, step_b, db, namespace):
    code_a = classify(step_a)
    code_b = classify(step_b)
    
    for level in [1, 2, 3, 4]:
        from_val = code_a.level_code(level)
        to_val = code_b.level_code(level)
        upsert_edge(level, from_val, to_val, db, namespace)
```

## 三、检索

### 自动注入：仅 L1

构建 system prompt 时调用，只查 L1：

```python
def get_l1_hints(current_code, db, namespace, top_k=3):
    edges = query_edges(level=1, from_code=current_code.l1, db, namespace)
    return sorted(edges, key=lambda e: e.total_count, reverse=True)[:top_k]
```

输出注入格式（1~2 行）：
```
[记忆] 此步后常跟: FILE → API(8), FILE → FILE(5)
```

### 钻取：memory_detail 工具

LLM 主动调用，查指定层：

```python
TOOL: memory_detail(level: 1|2|3|4)
  描述: 查看当前操作之后的执行流向。L1粗略，L4精确。
       从L1开始，不够具体就逐层深入。
```

## 四、层级定义

### L1 — 领域 Domain (8bit)

| Code | 领域 | 示例命令 |
|------|------|---------|
| 0x01 | FILE | dir, ls, type, cat |
| 0x02 | PROCESS | ps, tasklist, netstat |
| 0x03 | NETWORK | curl, ping |
| 0x04 | CODE | python, node, pip |
| 0x05 | API | MCP tools, HTTP API |
| 0x06 | SYSTEM | whoami, pwd, env |
| 0x08 | SEARCH | findstr, grep |
| 0x0F | UNKNOWN | 未匹配 |

### L2 — 动作 Action (8bit)

| L1=FILE | code | 
|---------|------|
| LIST | 0x01 |
| READ | 0x02 |
| WRITE | 0x03 |
| SEARCH | 0x04 |
| DELETE | 0x05 |
| META | 0x06 |

| L1=API | code |
|--------|------|
| LIST | 0x01 |
| GET | 0x02 |
| CREATE | 0x03 |
| UPDATE | 0x04 |
| DELETE | 0x05 |
| ACTION | 0x06 |

### L3 — 资源 Resource (32bit)

xxhash32 关键资源特征：
- FILE: hash(文件扩展名)，如 `.py` → `0xA3B7C92D`
- API: hash(资源名)，如 `tasks` → `0x1F4E8A2C`
- PROCESS: hash(进程名)，如 `python` → `0xC8D92E11`

### L4 — 细节 Detail (16bit)

- 高 8bit: HTTP method (GET=1, POST=2, PUT=3, DELETE=4)
- 低 8bit: 标志位 (CACHED=0x01, DANGEROUS=0x02, VERBOSE=0x04)

## 五、分类器（classifier.py）

纯规则，无 LLM：

```python
def classify(step) -> OpCode:
    method = step.get("method", "").upper()
    path = (step.get("path") or "").strip()
    cmd = path.split()[0].lower() if path else ""
    
    if method in ("GET", "POST", "PUT", "DELETE"):
        resource = parse_api_path(path)
        return OpCode(method, path, 5, http_to_l2(method), hash32(resource), http_flag(method))
    
    if method == "EXEC":
        return _classify_shell(cmd, path)
    
    return OpCode(method, path, 0x0F, 0, 0, 0)
```

## 六、注入控制

| 轮次 | 注入内容 |
|------|---------|
| Round 1 | 无 |
| Round 2+ | 有 L1 数据则 1 行 |
| 连续 3 轮工具调用无进展 | 追加提示 "(可用 memory_detail 查看历史模式)" |
| LLM 调用 memory_detail | 返回指定层的完整候选 |

## 七、兼容

- 旧 `agent_memory_edges` 表继续存在，读时忽略
- `retrieve_similar_flows` (卡片语义检索) 不变
- `suggest_next_nodes` 改为调用 `drill_down` 实现
