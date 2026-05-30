# 记忆系统事件触发链路

## chat_stream_events() 完整流程

```
用户输入 user_message
       |
       v
┌─────────────────────────────────────────────────┐
│ ① build_agent_messages_with_history()           │
│    ├── retrieve_similar_flows()  ← 记忆检索     │
│    │    每次构建消息时自动触发                    │
│    │    注入「历史相似流程」到 system prompt       │
│    └── 拼装完整 messages                         │
└─────────────────────────────────────────────────┘
       |
       v
┌─────────────────────────────────────────────────┐
│ ② LLM.chat(messages, tools)                     │
└─────────────────────────────────────────────────┘
       |
       v
       ┌──── 有 tool_calls? ────┐
       │                        │
       │ YES                    │ NO (纯文本回复)
       │                        │
       v                        v
┌────────────────────┐   ┌──────────────────────┐
│ ③ 处理每个 tool    │   │ ⑤ yield text_stream  │
│    ├── dispatch    │   │                      │
│    ├── all_steps   │   │ ⑥ 存 DB: assistant   │
│    │   _out 追加   │   │                      │
│    └── continue    │   │ ⑦ if all_steps_out:  │
│       (回到②)      │   │    ├── record_       │
└────────────────────┘   │    │   successful_    │
                         │    │   flow()         │
                         │    │   └→ 写入 Card    │
                         │    │                  │
                         │    ├── update_graph_  │
                         │    │   from_steps()   │
                         │    │   └→ 写入 Edge    │
                         │    │                  │
                         │    ├── Hook: on_memory│
                         │    │   _extract()     │
                         │    │   └→ 写入        │
                         │    │      memory.md   │
                         │    │                  │
                         │    └── yield "done"   │
                         └──────────────────────┘
```

## 事件 → 触发点对照

| 操作 | 触发事件 | 触发条件 | 代码位置 |
|---|---|---|---|
| **Card 检索** | 构建消息时 | 每次用户输入 | messages.py L36 |
| **Card 写入** | 纯文本回复后 | all_steps_out 有内容 | agent.py L325-330 |
| **Graph 边写入** | 纯文本回复后 | all_steps_out 有内容 | agent.py L331 |
| **memory.md 注入** | 组装 system prompt 时 | 每次对话开始 | assemble_sys_prompt() |
| **memory.md 写入** | 纯文本回复后 | Hook on_memory_extract 返回内容 | agent.py (待实现) |
| **memory.md 写入** | Agent 自主调用 | memory_write tool | builtins (待实现) |

## 关键条件

- all_steps_out 只在 **调过 tool** 的回合才有内容
- 纯聊天的对话（无 tool call）→ 不触发 Card/Graph 写入
- Card 写入是在 **最后一次 LLM 回复** 后才执行，不是在每轮 tool call 后立即执行
- 检索是 **每次对话开始** 都执行，不管有没有历史记忆

## 待实现的注入点

| 钩子 | 位置 | 作用 |
|---|---|---|
| `on_memory_extract(user_msg, reply, steps)` | agent.py L331 之后 | 规则提取 -> memory.md |
| `memory_write tool` | builtins.py | LLM 自主写入 memory.md |
| `enrich_card_with_experience(card)` | cards.py record_successful_flow 内 | LLM 提取踩坑 -> Card |
