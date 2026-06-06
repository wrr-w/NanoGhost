# Memory Hook 流程规则 SPEC

> 核心: LLM 只在「生成内容」时介入，不参与判断。
> Graph 只摆选项，不排序不推荐。

---

## 一、规则的本质

每条规则是一个流程节点：

```
在 [阶段/环节]
当 [条件] 发生时
走 [分支 A 或 B]
如果分支需要 LLM - 调 LLM 生成内容
如果不需要 - 直接执行
```

LLM 的角色只是内容生成器，不是决策者，也不是排序器。

---

## 二、完整流程

### Phase 0: 入口条件

```
[对话回合结束]
  条件: LLM 返回了纯文本回复 + 有步骤记录 (all_steps_out 非空)
  如果条件不满足 - 整个流程跳过
```

### Phase 1: Card + Graph 写入

```
[回合结束]
   |
   +-- record_successful_flow() -> 写 Card
   |   (flow_hash + intent + steps + embedding)
   |
   +-- update_graph_from_steps() -> 写 Graph
   |   (从 steps 切相邻对，写 L1~L4 四层)
   |
   +-- 等所有步骤结束后
   |   之前所有步骤的结果已经写入 Card 和 Graph
   |
   +-- LLM 总结经验 -> 写 Card.experience_notes
       (所有 tool call 结束后调一次，LLM 看到完整的
        intent + steps + reply 来做总结)
```

### Phase 2: Card 经验总结

**时机**：所有 tool calls **全部结束**、`record_successful_flow()` 已写入 Card 之后，**调一次** LLM 生成经验文本。

不做步骤级失败检测，不做 pitfall 概念，不逐步骤调 LLM。

```
# 输入：intent_summary + steps + reply
# 输出：一段经验文本

prompt = f"""
你刚完成了以下任务：
意图: {intent}
执行步骤: {steps_summary}
你的回复: {reply}

请用一段话总结执行该任务的经验，包括注意事项、常见坑、标准操作顺序等。
如果这次执行很顺畅没有任何值得记的，请输出"。
"""
```

### Phase 3: memory.md - Agent 自主写入

不做任何内容层自动提取。两个 section 全部由 Agent 通过 memory_write tool 自主写入：

```
# 任务完成后
memory_write(action="append", section="daily_log", ...)

# 讨论/脑暴中有关键结论
memory_write(action="append", section="decisions", ...)

# 用户说"记下这个"
memory_write(action="append", section="decisions", ...)
```

---

## 三、流程总图

```
回合结束 (有 tool calls)
   |
   +-- 1. Card 写入 (record_successful_flow)
   |   +-- flow_hash + intent + steps 自动写入
   |
   +-- 2. Graph 写入 (update_graph_from_steps)
   |   +-- 遍历 steps 相邻对，classify() -> L1~L4
   |   +-- 每层 upsert 边，count += 1
   |
   +-- 3. LLM 经验总结（每次都调）
   |   +-- 输入: intent + steps + reply
   |   +-- 输出: 一段经验文本，或空
   |   +-- 去重 -> card.experience_notes
   |
   +-- 4. memory.md LLM 写入 (Agent 自主)
       +-- memory_write -> daily_log
       +-- memory_write -> decisions
```

---

## 四、LLM 参与节点

```
节点 A: 经验总结 (Phase 2)
  触发: 每次 flow 完成 (有 tool calls + 有回复)
  输入: intent_summary + steps_summary + reply
  输出: 一段经验文本 -> card.experience_notes
  类型: 内容生成

节点 B: memory.md 维护
  触发: Agent 自主判断
  输入: 当前对话上下文
  输出: daily_log / decisions 内容
  类型: 内容生成（不是判断）
```

LLM 不做的事：
- 判断一条信息该不该记 -> Agent 自主判断
- 判断步骤有没有失败 -> 由规则检查 ok 字段决定（已取消 pitfall 检测）
- 判断是不是重试 -> 由规则检查相邻步骤决定（已取消 pitfall 检测）
- 排序/推荐 Graph 的出边 -> Graph 只摆选项，LLM 用意图自己选
- 判断这个值不值得记 -> LLM 在经验总结时判断

---

## 五、实现要点

```python
def process_post_turn_memory(user_message, reply, steps, db, llm, namespace):
    if not steps:
        return  # Phase 0: 无步骤跳过

    # Phase 1: Card + Graph 写入
    flow_hash = record_successful_flow(user_message, steps, ..., db=db, namespace=namespace)
    update_graph_from_steps(steps, db=db, namespace=namespace)
    if not flow_hash:
        return

    # Phase 2: LLM 经验总结（每次都调）
    card = load_card_by_hash(db, flow_hash, namespace)
    experience = llm_summarize_flow(llm, card.intent_summary, card.steps, reply)
    if experience and experience not in card.experience_notes:
        card.experience_notes.append(experience)
    save_card(db, card, namespace)

    # Phase 3: memory.md
    # 由 Agent 自主调用 memory_write tool，不在此处自动触发
```

---

## 六、对照（设计变更记录）

| 旧设计 | 新设计 |
|--------|--------|
| Graph 有评分排序逻辑 | Graph 只摆选项，不排序 |
| Graph 有剪枝 | Graph 保留所有边 |
| Graph 有 approved_count | 删除，只用 total_count |
| 有 pitfall 概念 | 删除，合并到 experience |
| experience 每 5 次总结一次 | 每次 flow 完成都总结 |
| memory.md 有自动提取 | 全部由 Agent 自主写入 |
| Card 和 Graph 代码上无关 | Card.steps 是 Graph 唯一数据源 |
