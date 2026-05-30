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

### Phase 4: memory.md 写入

```
[Card/Graph 写入后]
   ├─ 检查 user_message
   │   ├─ 包含"我叫""叫我"等自称结构?
   │   │   ├─ YES → 提取称呼 → memory.md "user_info" section
   │   │   │       → 不调 LLM，固定字符串拼接
   │   │   └─ NO  → 跳过
   │   └─ 包含"喜欢""不要""倾向"等偏好结构?
   │       ├─ YES → 提取偏好 → memory.md "preference" section
   │       │       → 不调 LLM，固定字符串拼接
   │       └─ NO  → 跳过
   │
   ├─ 检查 reply
   │   ├─ 包含"建议""推荐""注意"等指导性语言?
   │   │   ├─ YES → 提取建议文本 → memory.md "experience" section
   │   │   │       → 不调 LLM，固定提取 reply 中的原文
   │   │   └─ NO  → 跳过
   │   └─ 跳过
   │
   └─ 检查 steps 中的 EXEC 命令
       ├─ 命令是 dir/pwd/ls 等路径查询?
       │   ├─ YES → 输出中包含路径?
       │   │   ├─ YES → 提取路径 → memory.md "project_context" section
       │   │   │       → 不调 LLM，正则提取
       │   │   └─ NO  → 跳过
       │   └─ NO → 跳过
```

---

## 三、LLM 只在两个节点介入

```
节点 A: 经验总结 (Phase 3)
  触发: card.success_count >= 3 AND % 5 == 0
  输入: card.steps + card.intent_summary
  输出: 一段经验文本 → card.experience_notes
  类型: 内容生成（不是判断）

节点 B: (未来) 记忆维护
  触发: memory.md > 150 行 / 每天一次
  输入: 当前 memory.md 全文
  输出: 精简后的 memory.md
  类型: 内容生成（不是判断）
```

**LLM 不做的事：**
- ❌ 判断一条信息该不该记 → 流程规则决定
- ❌ 判断用户说的是不是偏好 → 检查消息结构决定
- ❌ 判断步骤有没有失败 → 检查 ok 字段决定
- ❌ 判断是不是重试 → 检查相邻步骤决定
- ❌ 判断回复里有没有建议 → 检查关键词决定

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
   └─ 4. memory.md
       ├─ 消息含自称? → 记称呼 (模板)
       ├─ 消息含偏好? → 记偏好 (模板)
       ├─ 回复含建议? → 记经验 (原文)
       ├─ 命令出路径? → 记路径 (提取)
       └─ 都不满足 → 跳过
```

---

## 五、实现要点

```python
# 这个函数就是所有的"规则"
# 没有模式匹配，只有流程分支
# LLM 只在两个 if 分支里被调用

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
        # 分支节点: 步骤是否失败?
        if not steps[i].get("ok"):
            # 分支节点: 下一步是否同路径重试成功?
            if steps[i+1].get("ok") and same_endpoint(steps[i], steps[i+1]):
                text = make_pitfall_text(steps[i])  # 固定模板，不调 LLM
                if text not in card.pitfalls:
                    card.pitfalls.append(text)
            else:
                # 分支节点: 是否有已知错误模式?
                error_type = match_error_type(steps[i])  # 查表，不调 LLM
                if error_type:
                    text = make_pitfall_text(steps[i], error_type)  # 固定模板
                    if text not in card.pitfalls:
                        card.pitfalls.append(text)

    # Phase 3: LLM 经验总结 (LLM 唯一入口)
    if card.success_count >= 3 and card.success_count % 5 == 0:
        experience = llm_summarize_flow(llm, card)  # LLM 生成内容
        if experience not in card.experience_notes:
            card.experience_notes.append(experience)

    # Phase 4: memory.md
    entries = []
    name = extract_self_referral(user_message)  # 字符串判断，不调 LLM
    if name:
        entries.append({"section": "user_info", "content": f"- Name: {name}"})
    pref = extract_preference_keywords(user_message)  # 字符串判断
    if pref:
        entries.append({"section": "preference", "content": pref})
    advice = extract_advice_sentences(reply)  # 字符串判断
    if advice:
        entries.append({"section": "experience", "content": advice})
    path = extract_path_from_steps(steps)  # 正则提取
    if path:
        entries.append({"section": "project_context", "content": path})
    if entries:
        append_to_memory_md(entries)

    save_card(db, card, namespace)
```

---

## 六、对照

| 之前错的理解 | 正确的理解 |
|---|---|
| 规则 = 正则匹配文字 | 规则 = 流程节点上的分支 |
| LLM 判断信息价值 | LLM 不判断，流程决定 |
| LLM 提取偏好语义 | 检查消息结构，不调 LLM |
| 多层路由架构 | 一条直线流程，遇到分支走条件 |
| LLM 每轮都可能调 | LLM 只在 5 次阈值时调 |
