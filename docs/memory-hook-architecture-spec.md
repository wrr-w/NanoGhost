# Memory Hook 架构 SPEC

> 核心问题: Hook 之后怎么拆？什么时候 LLM 介入？什么时候不介入？

---

## 一、架构总览：三层路由

```
Hook 触发 (每次对话回合结束后)
        |
        v
┌─────────────────────────────┐
│  Layer 1: Rule Engine       │  ← 同步，即时，无 LLM
│  固定规则匹配                 │
│  (正则 / 状态判断 / 关键词)   │
│                             │
│  输出: 确定的 memory 操作     │
└──────────┬──────────────────┘
           | 通过
           v
┌─────────────────────────────┐
│  Layer 2: 路由决策           │  ← 决定谁处理
│                             │
│  走规则已处理的 → 直接写入     │
│  走 LLM 的      → 进入 Batch │
│  不需要记的     → 丢弃        │
└──────────┬──────────────────┘
           | 需要 LLM
           v
┌─────────────────────────────┐
│  Layer 3: LLM Engine        │  ← 异步，批处理，有 LLM
│  语义理解 / 总结 / 合并       │
│                             │
│  触发条件:                   │
│  - 积累了 N 条待处理           │
│  - 或执行次数达到阈值          │
│  - 或 idle 超时强制刷新        │
└─────────────────────────────┘
```

---

## 二、Layer 1: Rule Engine (无 LLM)

每条规则有明确的输入、判断条件、输出。不调 LLM，纯逻辑。

### R1: 步骤失败重试检测

```python
def rule_retry_pattern(steps: list) -> list[Pitfall]:
    """判断: 同一步骤失败后重试成功"""
    result = []
    for i in range(len(steps)-1):
        a, b = steps[i], steps[i+1]
        if (not a.get("ok")) and b.get("ok")            and a.get("method") == b.get("method")            and a.get("path") == b.get("path"):
            result.append(Pitfall(
                target="card",  # 写进 Card
                step_num=i+1,
                text=f"{a.get('method')} {a.get('path')} 首次失败重试后成功",
                category="unstable"
            ))
    return result
```

### R2: Error 关键词匹配

```python
ERROR_PATTERNS = [
    (r"login|auth|token|credential", "auth", "需要重新认证"),
    (r"timeout|timed ?out", "timeout", "建议增大超时时间或分批执行"),
    (r"rate.?limit|429", "rate_limit", "触发频率限制，建议加 delay"),
    (r"not.?found|404", "not_found", "资源可能不存在，先查询确认"),
    (r"connect|refused|unreachable", "network", "服务可能未启动，先检查状态"),
    (r"permission|denied|forbidden|403", "permission", "权限不足"),
]

def rule_error_keyword(steps: list) -> list[Pitfall]:
    """判断: 步骤出错且有已知模式的错误信息"""
    result = []
    for s in steps:
        if s.get("ok") is not False:  # 没失败的步骤不检查
            continue
        preview = (s.get("result_preview") or "") + (s.get("error") or "")
        for pat, cat, suggestion in ERROR_PATTERNS:
            if re.search(pat, preview, re.IGNORECASE):
                result.append(Pitfall(
                    target="card",
                    step_num=s.get("step", 0),
                    text=f"{s.get('method')} {s.get('path')}: {suggestion}",
                    category=cat
                ))
                break  # 每个步骤只记一条
    return result
```

### R3: 路径提取

```python
PATH_CMDS = {"dir", "pwd", "where", "ls", "cd", "find", "echo %cd%"}
def rule_extract_path(steps: list) -> list[MemoryEntry]:
    """判断: terminal 命令输出了项目路径"""
    result = []
    for s in steps:
        if s.get("method") != "EXEC":
            continue
        cmd = (s.get("path") or "").strip()
        cmd_name = cmd.split()[0] if cmd else ""
        if cmd_name not in PATH_CMDS:
            continue
        for line in (s.get("result_preview") or "").split("\n"):
            line = line.strip()
            if re.match(r"^[A-Z]:\\", line):  # Windows 绝对路径
                result.append(MemoryEntry(
                    target="memory_md",
                    section="project_context",
                    content=f"- Path: {line}"
                ))
                break  # 每个步骤只取第一个路径
    return result
```

### R4: 用户偏好提取

```python
PREF_PATTERNS = [
    # 优先级从高到低
    (r"叫我[了]?[：: ]?(.+)", "user_info", "Name: {0}"),
    (r"我[是叫](.+?)[，。]?", "user_info", "Name: {0}"),
    (r"喜欢(.+?)(?:的回复|的风格|的回答)", "preference", "Style: likes {0}"),
    (r"不要(.+?)(?:了|哦|哈|吧|呀)?[。！]?", "preference", "Avoid: {0}"),
    (r"用(.+?)代替(.+?)", "preference", "Prefer {0} over {1}"),
    (r"我(?:的)?(?:习惯|偏好)是(.+)", "preference", "Habit: {0}"),
]

def rule_extract_preference(user_msg: str) -> MemoryEntry | None:
    """判断: 用户说了个人偏好"""
    for pat, section, tmpl in PREF_PATTERNS:
        m = re.search(pat, user_msg)
        if m:
            content = tmpl.format(*m.groups())
            return MemoryEntry(
                target="memory_md",
                section=section,
                content=f"- {content}"
            )
    return None
```

### R5: 回复建议提取

```python
ADVICE_PATTERNS = [
    (r"建议(.+?)(?:[。，.!]|$)", "experience"),
    (r"以后(.+?)(?:[。，.!]|$)", "experience"),
    (r"标准(?:流程|做法)(.+?)(?:[。，.!]|$)", "experience"),
    (r"推荐(.+?)(?:[。，.!]|$)", "experience"),
]

def rule_extract_advice(reply: str) -> MemoryEntry | None:
    """判断: LLM 回复中包含了操作建议"""
    for pat, section in ADVICE_PATTERNS:
        m = re.search(pat, reply)
        if m:
            return MemoryEntry(
                target="memory_md",
                section=section,
                content=f"- {m.group(1).strip()}"
            )
    return None
```

### R6: 去重

```python
def rule_dedup(existing: list[str], new_items: list[str]) -> list[str]:
    """判断: 新内容是否已存在（精确匹配 + 模糊匹配）"""
    result = []
    for item in new_items:
        if item in existing:
            continue  # 精确去重
        # 模糊去重: 关键路径相同则替换
        existing_normalized = [re.sub(r"[：:：].*", "", e) for e in existing]
        if re.sub(r"[：:：].*", "", item) in existing_normalized:
            continue
        result.append(item)
    return result
```

---

## 三、Layer 2: 路由决策

Rule Engine 处理完后，每个结果需要决定最终去向：

```python
@dataclass
class RouteDecision:
    target: str       # "card" | "memory_md" | "discard"
    content: str
    section: str = ""
    priority: int = 0  # 0=规则确定, 1=需LLM确认

def route(result) -> RouteDecision:
    """路由决策 — 纯逻辑，无 LLM"""
    
    # 规则已确定的，直接写
    if isinstance(result, Pitfall) and result.target == "card":
        return RouteDecision(target="card", content=result.text, priority=0)
    
    if isinstance(result, MemoryEntry) and result.target == "memory_md":
        return RouteDecision(
            target="memory_md",
            content=result.content,
            section=result.section,
            priority=0
        )
    
    # 规则能处理但置信度低的 → 走 LLM 确认
    # 当前没有这类，所有规则都是确定性的
    
    # 规则完全处理不了的 → 走 LLM
    # 比如: 非结构化的对话内容是否值得记
    return RouteDecision(target="discard", content="", priority=1)
```

**核心原则：**

| 情况 | 路由 | 理由 |
|---|---|---|
| 规则匹配成功 | 直接写入 | 确定无误，不需 LLM |
| 规则匹配失败但对话中有潜在价值 | 进 Batch 等 LLM | 不确定，让 LLM 判断 |
| 规则匹配失败且对话无价值 | 丢弃 | 无关信息不处理 |

---

## 四、Layer 3: LLM Engine

LLM 只在以下场景介入，且是**批处理**不是每轮都调：

### 触发条件

```python
class LLMBatch:
    """LLM 批处理队列"""
    max_batch: int = 10       # 攒够 10 条才调
    min_interval: int = 300   # 距上次至少 5 分钟
    force_check_interval: int = 3600  # 最多 1 小时强制跑一次
```

### LLM 处理的场景

```
场景 A: 流程经验总结 (R3 in spec v3)
  触发: 某 Card success_count % 5 == 0
  LLM: 总结这个流程的通用经验
  输出: card.experience_notes.append(...)

场景 B: 对话价值判断
  触发: Batch 积累了 N 条对话回合 (user_msg + reply)
  LLM: "以下对话中是否有值得记住的信息？如有，归类输出"
  输出: memory.md entries

场景 C: 记忆维护
  触发: 每天一次 / memory.md 超过 150 行
  LLM: 合并同类项、删除过时信息
  输出: 精简后的 memory.md
```

### LLM 不介入的场景（全部用规则）

```
❌ R1 重试检测          → 状态判断就够了
❌ R2 错误关键词         → 正则匹配就够了
❌ R3 路径提取           → 正则就够了
❌ R4 用户偏好关键词      → 正则就够了
❌ R5 回复建议关键词      → 正则就够了
❌ R6 去重              → 字符串比较就够了
❌ 路由决策              → 规则引擎的结果是确定的
```

---

## 五、完整调用链路

```python
# agent.py: 每次对话回合结束后的记忆处理

def _process_memory(self, user_message, reply, steps, session_id):
    """三层记忆处理流水线"""

    # ── Phase 1: Card + Graph (现有逻辑) ──
    if steps:
        flow_hash = record_successful_flow(user_message, steps, ...)
        update_graph_from_steps(steps, ...)

    # ── Phase 2: Rule Engine (新增，无 LLM) ──
    if not flow_hash:
        return

    card = find_card(flow_hash)
    
    # 2a: 规则提取 pitfall -> Card
    new_pitfalls = []
    new_pitfalls.extend(rule_retry_pattern(steps))
    new_pitfalls.extend(rule_error_keyword(steps))
    new_pitfalls = rule_dedup(card.pitfalls, new_pitfalls)
    if new_pitfalls:
        card.pitfalls.extend(new_pitfalls)
        save_card(card)

    # 2b: 规则提取 -> memory.md
    new_entries = []
    pref = rule_extract_preference(user_message)
    if pref: new_entries.append(pref)
    advice = rule_extract_advice(reply)
    if advice: new_entries.append(advice)
    paths = rule_extract_path(steps)
    new_entries.extend(paths)
    if new_entries:
        append_to_memory_md(new_entries)

    # 2c: 检测是否需要 LLM 总结 (R3)
    if card.success_count >= 3 and card.success_count % 5 == 0:
        add_to_llm_batch({"type": "card_summary", "card": card})

    # ── Phase 3: LLM Engine (异步/批处理) ──
    self._llm_batcher.add({
        "user_message": user_message,
        "reply": reply,
        "has_pitfalls": bool(new_pitfalls)
    })
    # _llm_batcher 在后台线程中攒够了再调 LLM
```

---

## 六、总结

```
                Hook 触发
                    |
           ┌────────┴────────┐
           |                 |
      规则能处理           规则不能处理
           |                 |
      ┌────┴────┐           |
      |         |           |
  写入 Card  写入        攒进 Batch
  (pitfall)  memory.md      |
      |         |           |
      └────┬────┘           |
           |                |
        路由决策 ───────── 够数了?
           |              /            结束            YES    NO
                        |      |
                    调 LLM    等待
                   总结/提取
```

**要不要 LLM 介入的判断标准只有一条：规则能确定的事，永远不走 LLM。**
