# Memory Hook 流程规则 SPEC

> 核心: 规则是流程分支，不是文字匹配。LLM 只在「生成内容」时介入，不参与判断。

---

## 一、规则的本质

每条规则是一个流程节点：

```
在 [阶段/环节]
当 [条件] 发生时
走 [分支 A 或 B]
如果分支需要 LLM → 调 LLM 生成内容
如果不需要 → 直接执行
```

LLM 的角色只是**内容生成器**，不是**决策者**。

---

## 二、完整流程

### Phase 0: 入口条件

```
[对话回合结束]
  条件: LLM 返回了纯文本回复 + 有步骤记录 (all_steps_out 非空)
  如果条件不满足 → 整个流程跳过
```

### Phase 1: Card + Graph 写入 (已有逻辑)

```
[回合结束]
   ├─ record_successful_flow() → 写 Card
   └─ update_graph_from_steps() → 写 Graph
```

### Phase 2: Card Pitfall 判断 (新增)

```
[Card 写入后]
   ├─ 遍历 steps
   │   └─ 步骤 i:
   │       ├─ step[i].ok == False?
   │       │   ├─ YES → step[i+1].ok == True?
   │       │   │   ├─ YES → step[i].method/path == step[i+1].method/path?
   │       │   │   │   ├─ YES → 重试成功模式
   │       │   │   │   │       → 生成 pitfall 文本 (不调 LLM，固定模板)
   │       │   │   │   │       → card.pitfalls.append(...)
   │       │   │   │   └─ NO  → 错误后用其他方式解决
   │       │   │   │           → 生成 pitfall 文本 (不调 LLM，固定模板)
   │       │   │   │           → card.pitfalls.append(...)
   │       │   │   └─ NO  → 步骤失败未恢复 → 跳过 (不记)
   │       │   └─ NO → step[i].ok == True，继续下一个
   │       │
   │       └─ step[i].ok == False?
   │           ├─ YES → 检查 result_preview 中是否有已知错误模式
   │           │   ├─ YES → 映射到预定义的错误类别
   │           │   │       → 生成 pitfall 文本 (不调 LLM，查表)
   │           │   │       → card.pitfalls.append(...)
   │           │   └─ NO  → 跳过
   │           └─ NO → 跳过
   │
   └─ pitfall 去重
       → 精确匹配已有 pitfalls
       → 有重复 → 跳过
       → 无重复 → card.pitfalls.append(...)
```

### Phase 3: Experience 总结 (需要 LLM)

```
[Card 写入后]
   ├─ card.success_count >= 3?
   │   ├─ YES → card.success_count % 5 == 0?
   │   │   ├─ YES → 需要 LLM 生成经验总结
   │   │   │       → 输入: card.steps + card.intent_summary
   │   │   │       → LLM 生成: "执行该流程的注意事项"
   │   │   │       → card.experience_notes.append(LLM 输出)
   │   │   └─ NO  → 跳过
   │   └─ NO → 跳过
```

### Phase 4: memory.md — Agent 自主写入

不做任何内容层自动提取（无正则、无关键词匹配）。
两个 section 全部由 Agent 通过 memory_write tool 自主写入：

```
# 任务完成后
memory_write(action="append", section="daily_log", ...)

# 讨论/脑暴中有关键结论
memory_write(action="append", section="decisions", ...)

# 用户说"记下这个"
memory_write(action="append", section="decisions", ...)
```

### Phase 5: 对照

| 层 | 写入方式 | LLM 参与 |
|----|---------|-----------|
| daily_log | memory_write tool | ✅ Agent 判断 |
| decisions | memory_write tool | ✅ Agent 判断 |

---

## 三、LLM 参与节点

```
节点 A: 经验总结 (Phase 3)
  触发: card.success_count >= 3 AND % 5 == 0
  输入: card.steps + card.intent_summary
  输出: 一段经验文本 → card.experience_notes
  类型: 内容生成（不是判断）

节点 B: memory.md 维护（未来）
  触发: memory.md > 150 行 / 每天一次
  输入: 当前 memory.md 全文
  输出: 精简后的 memory.md
  类型: 内容生成（不是判断）
```

LLM 不做的事：
- ❌ 判断一条信息该不该记 → 流程规则决定
- ❌ 判断用户说的是不是偏好 → 检查消息结构决定
- ❌ 判断步骤有没有失败 → 检查 ok 字段决定
- ❌ 判断是不是重试 → 检查相邻步骤决定
- ❌ 判断回复里有没有建议 → 检查关键词决定
- ❌ 判断"这个值不值得记" → Agent 自主判断

---

## 四、流程总图

```
回合结束 (有 tool calls)
   │
   ├─ 1. Card + Graph 写入 (已有)
   │
   ├─ 2. Card Pitfall
   │   ├─ 遍历 steps
   │   │   ├─ 失败→重试成功? → 记 pitfall (模板)
   │   │   ├─ 失败→有已知错误? → 记 pitfall (查表)
   │   │   └─ 都不满足 → 跳过
   │   └─ 去重 → 写入 card
   │
   ├─ 3. Card Experience
   │   ├─ 累计 5 次? → LLM 生成经验总结 → 写入 card
   │   └─ 不到 5 次 → 跳过
   │
   ├─ 4. memory.md 自动提取
   │   ├─ 消息含自称? → user_info
   │   ├─ 消息含偏好? → preference
   │   ├─ 回复含建议? → experience
   │   └─ 命令出路径? → project_context
   │
   └─ 5. memory.md LLM 写入 (Agent 自主)
       ├─ memory_write -> daily_log
       └─ memory_write -> decisions
```

---

## 五、实现要点

```python
def process_post_turn_memory(user_message, reply, steps, db, llm, namespace):
    """回合结束后的记忆处理 — 流程规则引擎"""
    if not steps:
        return  # Phase 0: 无步骤跳过

    # Phase 1: Card + Graph (已有)
    flow_hash = record_successful_flow(user_message, steps, ..., db=db, namespace=namespace)
    update_graph_from_steps(steps, ..., db=db, namespace=namespace)
    if not flow_hash:
        return

    card = load_card_by_hash(db, flow_hash, namespace)

    # Phase 2: Pitfall 检测
    for i in range(len(steps) - 1):
        if not steps[i].get("ok"):
            if steps[i+1].get("ok") and same_endpoint(steps[i], steps[i+1]):
                text = make_pitfall_text(steps[i])
                if text not in card.pitfalls:
                    card.pitfalls.append(text)
            else:
                error_type = match_error_type(steps[i])
                if error_type:
                    text = make_pitfall_text(steps[i], error_type)
                    if text not in card.pitfalls:
                        card.pitfalls.append(text)

    # Phase 3: LLM 经验总结
    if card.success_count >= 3 and card.success_count % 5 == 0:
        experience = llm_summarize_flow(llm, card)
        if experience not in card.experience_notes:
            card.experience_notes.append(experience)

    # Phase 4: memory.md 自动提取
    entries = []
    name = extract_self_referral(user_message)
    if name:
        entries.append({"section": "user_info", "content": f"- Name: {name}"})
    pref = extract_preference_keywords(user_message)
    if pref:
        entries.append({"section": "preference", "content": pref})
    advice = extract_advice_sentences(reply)
    if advice:
        entries.append({"section": "experience", "content": advice})
    path = extract_path_from_steps(steps)
    if path:
        entries.append({"section": "project_context", "content": path})
    if entries:
        append_to_memory_md(entries)

    save_card(db, card, namespace)
    # Phase 5: memory.md LLM 写入 (daily_log, decisions)
    # 由 Agent 自主调用 memory_write tool，不在此处触发

```

---

## 六、memory.md 层级对照

| 层 | 内容 | 写入方式 | LLM 参与 |
|----|------|---------|-----------|
| user_info | 称呼、偏好 | 自动提取 | ❌ |
| daily_log | 每日完成事项 | memory_write | ✅ Agent 判断 |
| decisions | 关键结论、设计决策 | memory_write | ✅ Agent 判断 |
| project_context | 项目路径 | 自动提取 | ❌ |
| experience | 建议原文 | 自动提取 | ❌ |
## 六、对照

| 之前错的理解 | 正确的理解 |
|---|---|
| 规则 = 正则匹配文字 | 规则 = 流程节点上的分支 |
| LLM 判断信息价值 | LLM 不判断，流程决定 |
| LLM 提取偏好语义 | 检查消息结构，不调 LLM |
| 多层路由架构 | 一条直线流程，遇到分支走条件 |
| LLM 每轮都可能调 | LLM 只在 5 次阈值时调 |
