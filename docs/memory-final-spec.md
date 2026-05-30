# 记忆系统 - 最终执行 SPEC

> 三套存储：Graph / Card / memory.md
> 每个节点标注：是否需要 LLM

---

## 一、信息流总图

```
工具调用 (MCP / Skill / Terminal / HTTP GET/POST)
       │
       │ 步骤数据 (method, path, ok, result_preview, error)
       │
       ├──────────────────────────────────────────┐
       │                                          │
       ▼                                          ▼
  ┌──────────┐                            ┌──────────────┐
  │  Graph   │                            │     Card     │
  │  不需要   │                            │  部分需要 LLM  │
  │  LLM     │                            │              │
  │          │                            │  steps:      │
  │  记边:   │                            │  自动填入     │
  │  A→B→C   │                            │  (不需要 LLM) │
  │  计数+1  │                            │              │
  │          │                            │  pitfalls:   │
  │          │                            │  异常时 LLM   │
  │          │                            │  生成        │
  │          │                            │              │
  │          │                            │  experiences:│
  │          │                            │  多次后 LLM   │
  │          │                            │  生成        │
  └──────────┘                            └──────────────┘
                                                 │
                                          (同时影响用户)
                                                 │
                                                 ▼
                                          ┌──────────────┐
                                          │  memory.md   │
                                          │              │
                                          │  user_info:  │
                                          │  字符串提取   │
                                          │  (不需要 LLM) │
                                          │              │
                                          │  preference: │
                                          │  字符串提取   │
                                          │  (不需要 LLM) │
                                          │              │
                                          │  project_ctx:│
                                          │  路径提取     │
                                          │  (不需要 LLM) │
                                          │              │
                                          │  tips:       │
                                          │  截取原文     │
                                          │  (不需要 LLM) │
                                          └──────────────┘
```

---

## 二、Graph — 纯结构，不需要 LLM

### 存什么

```
边: (method_A, path_A) -> (method_B, path_B)
属性: count, relation_type="FOLLOWS"

例子:
  POST /api/tasks -> POST /api/task/settings  count=5
  POST /api/task/settings -> POST /api/task/start  count=4
  POST /api/tasks -> POST /api/task/start  count=1  (跳过 settings)
```

### 写入规则

```
每完成一轮 tool call 后
  遍历 steps
  对每对相邻步骤 (i, i+1):
    edge.from_method = steps[i].method
    edge.from_path = steps[i].path
    edge.to_method = steps[i+1].method
    edge.to_path = steps[i+1].path
    edge.count += 1
    edge.relation = "FOLLOWS"
```

### 读取规则

```
在 build_agent_messages() 中:
  从当前 steps 的最后一步查 graph
  返回高频后续步骤
  注入: "根据历史，{method} {path} 之后通常做 {next_method} {next_path}"
```

---

## 三、Card — 部分需要 LLM

### 存什么

```python
@dataclass
class Card:
    # 自动填充（不需要 LLM）
    flow_hash: str           # 步骤模式的 SHA256
    intent_summary: str      # 用户意图
    steps: list[Step]        # 步骤序列 (method, path, ok, status_code)
    intent_vector: list[float]  # Embedding
    success_count: int
    total_rounds: int

    # LLM 生成（需要 LLM）
    pitfalls: list[str]      # 踩坑文本
    experience_notes: list[str]  # 经验文本

    # 反馈
    approved_count: int
    rejected_count: int
    trigger_count: int
    namespace: str
```

### 写入规则 — 什么情况需要 LLM

```
┌─ 回合结束，有 steps ──────────────────────────┐
│                                                │
│  1. Card 本体 (flow_hash, steps, intent)       │
│     自动填充，不需要 LLM                         │
│                                                │
│  2. Pitfall 检测                                │
│     ├─ 有步骤 failed？                          │
│     │   ├─ YES → 需要 LLM:                       │
│     │   │   输入: 该步骤的 method, path,          │
│     │   │         result_preview, error          │
│     │   │   输出: 一段踩坑文本                    │
│     │   │   → card.pitfalls.append(text)         │
│     │   │                                         │
│     │   └─ NO → 跳过                              │
│     │                                             │
│     └─ 有步骤失败后重试成功？                      │
│         ├─ YES → 需要 LLM:                        │
│         │   输入: 两次步骤的结果对比               │
│         │   输出: 一段重试踩坑文本                 │
│         │   → card.pitfalls.append(text)          │
│         └─ NO → 跳过                              │
│                                                │
│  3. Experience 检测                              │
│     └─ success_count >= 3 且 % 5 == 0?          │
│         ├─ YES → 需要 LLM:                       │
│         │   输入: card 的全部 steps + intent      │
│         │   输出: 一段流程经验总结                │
│         │   → card.experience_notes.append(text) │
│         └─ NO → 跳过                             │
│                                                │
│  4. 写入 DB                                     │
└─────────────────────────────────────────────────┘
```

### LLM 输入/输出模板

#### Pitfall 模板

```
输入:
  你正在执行「{intent_summary}」流程。
  步骤 {step_num}: {method} {path}
  返回结果: {result_preview}
  错误信息: {error}

  请分析这个步骤失败的原因，写出 1-2 句踩坑提醒。
  要求:
  - 具体: 说明什么情况下会失败
  - 可操作: 给出解决建议
  - 简洁: 不超过 50 字

输出: {pitfall_text}
```

#### Experience 模板

```
输入:
  以下是一个流程的 {success_count} 次执行记录:
  意图: {intent_summary}
  步骤: {steps 摘要}

  请写出执行该流程的经验总结，包括:
  - 标准操作顺序
  - 需要注意的点
  - 常见的变体或分支

输出: {experience_text}
```

### 读取规则

```
在 retrieve_similar_flows() 返回时:
  附带 card.pitfalls, card.experience_notes

注入到 prompt:
  「【历史相似流程】
   {intent_summary}
   步骤: {method} {path} -> {method} {path} -> ...
   踩坑: {pitfalls[0]}, {pitfalls[1]}...
   经验: {experience_notes[0]}...」
```

---

## 四、memory.md — 不需要 LLM

### 存什么

```
{INSTANCE_DIR}/memory.md

# NanoGhost Memory

## user_info          ← 用户自称、称呼
- Name: 王

## preference         ← 用户偏好（对话中提取）
- Likes: 简洁回复
- Avoid: 自动执行写操作

## project_context    ← 项目环境信息
- Capture path: E:\...\Capture
- Capture port: 8000

## tips               ← 通用建议（从回复中截取）
- 操作前先检查服务状态
- 涉及修改先问用户

## error_workarounds  ← 已知错误处理
- Capture 的 /api/tasks 如果返回 500，重启服务即可
```

### 写入规则 — 全部不需要 LLM

```
┌─ 回合结束 ──────────────────────────┐
│                                      │
│  1. 检查 user_message                │
│     ├─ 含 "叫我" / "我是" / "叫我了"? │
│     │   ├─ YES → 截取后半段          │
│     │   │       → memory.md user_info │
│     │   │       → 不需要 LLM         │
│     │   └─ NO → 跳过                 │
│     │                                │
│     ├─ 含 "喜欢" / "不要" / "倾向"?   │
│     │   ├─ YES → 截取后半段          │
│     │   │       → memory.md preference│
│     │   │       → 不需要 LLM         │
│     │   └─ NO → 跳过                 │
│     │                                │
│  2. 检查 reply                       │
│     └─ 含 "建议" / "注意" / "推荐"?   │
│         ├─ YES → 截取后面的句子       │
│         │       → memory.md tips     │
│         │       → 不需要 LLM         │
│         └─ NO → 跳过                 │
│                                      │
│  3. 检查 steps 中的 EXEC 命令        │
│     └─ dir / pwd / where / ls?       │
│         ├─ YES → 输出含路径?         │
│         │   ├─ YES → 提取路径       │
│         │   │       → memory.md     │
│         │   │       project_context │
│         │   │       → 不需要 LLM    │
│         │   └─ NO → 跳过            │
│         └─ NO → 跳过                 │
│                                      │
│  4. (未来) 检查 Card 是否有新 pitfall │
│     └─ 有新 pitfall?                 │
│         ├─ YES → 截取 pitfall 文本   │
│         │       → memory.md         │
│         │       error_workarounds   │
│         │       → 不需要 LLM        │
│         └─ NO → 跳过                 │
│                                      │
└──────────────────────────────────────┘
```

### 判断逻辑示例

```python
# 检查 user_message 是否包含自称 — 不需要 LLM
def check_self_referral(text: str) -> str | None:
    for prefix in ["叫我", "我是", "叫我了"]:
        if text.startswith(prefix) or ("，" in text and prefix in text):
            # 提取"叫我小王"中的"小王"
            # 不需要 LLM，字符串操作
            idx = text.find(prefix) + len(prefix)
            name = text[idx:].split("。")[0].split("，")[0].split(" ")[0].strip()
            if name and len(name) <= 10:  # 名字不会太长
                return name
    return None

# 检查 reply 是否包含建议 — 不需要 LLM
def check_advice(text: str) -> str | None:
    for kw in ["建议", "注意", "推荐"]:
        if kw in text:
            idx = text.find(kw)
            # 取关键词后面的第一个句子
            sentence = text[idx:].split("。")[0].split("!")[0].strip()
            if len(sentence) <= 100:
                return sentence
    return None
```

---

## 五、LLM 调用汇总

| 节点 | 触发条件 | 输入 | 输出 | 频率 |
|---|---|---|---|---|
| Card pitfall (失败) | 步骤返回 error/failed | 该步骤的 method, path, 结果 | 1-2 句踩坑文本 | 每次失败时 |
| Card pitfall (重试) | 失败后重试成功 | 两次步骤的结果对比 | 1-2 句踩坑文本 | 每次重试时 |
| Card experience | success_count >= 3 且 % 5 == 0 | 卡片的全部步骤和意图 | 一段经验总结 | 每 5 次执行 |
| (未来) memory.md | 手动触发 | 当前 memory.md 全文 | 精简后的版本 | 每天一次 |

---

## 六、代码实施清单

| 步骤 | 文件 | 改动 | 需要 LLM? |
|---|---|---|---|
| 1 | memory/models.py | Card 加 pitfalls, experience_notes 字段 | 不涉及 |
| 2 | adapters/database.py | DB migration 加列 | 不涉及 |
| 3 | memory/cards.py | enrich_card_pitfalls() — 检测失败→调 LLM→写入 | ✅ 是 |
| 4 | memory/cards.py | enrich_card_experience() — 检测阈值→调 LLM→写入 | ✅ 是 |
| 5 | memory/cards.py | LLM 调用模板 (pitfall / experience) | 不涉及 |
| 6 | agent.py | 在 record_successful_flow 后调用 enrich | 不涉及 |
| 7 | agent.py | extract_memory_md_entries() — 纯字符串判断 | 不涉及 |
| 8 | agent.py | append_to_memory_md() — 文件写入 | 不涉及 |
| 9 | tool/builtins.py | memory_write tool | 不涉及 |
| 10 | engine/messages.py | 注入 pits/experiences 到 prompt | 不涉及 |
| 11 | run.py | assemble_sys_prompt 读 memory.md | 不涉及 |
| 12 | prompts/agent_profile.md | 加 memory 描述 | 不涉及 |
