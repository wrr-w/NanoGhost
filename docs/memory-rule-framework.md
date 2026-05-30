# 记忆系统操作规则框架

## 概述

两套记忆的操作不是随机触发的，而是遵循一套统一的决策规则。
这套规则定义了：什么信息、在什么时机、该进哪套记忆、怎么维护。

---

## 规则一：分类规则 — 信息该进哪套记忆？

```
一条信息
   |
   ├─ 跟具体操作流程绑定？  (例如：创建任务第2步容易超时)
   │     YES → Flow Cards (pitfalls / experience_notes)
   │
   ├─ 跟用户个人相关？      (例如：喜欢简洁回复)
   │     YES → memory.md (用户信息)
   │
   ├─ 跟项目/环境相关？     (例如：Capture 在 port 8000)
   │     YES → memory.md (项目上下文)
   │
   ├─ 是通用操作经验？      (例如：操作前先检查状态)
   │     YES → memory.md (经验总结)
   │
   └─ 以上都不是 → 不记
```

### 判定示例

| 原文 | 归属 | 理由 |
|---|---|---|
| settings 没配 timeout 会 30 秒超时 | Flow Card pitfall | 跟特定步骤绑定 |
| 创建任务的标准流程是 create -> settings -> start | Flow Card experience | 跟特定流程绑定 |
| 用户说"叫我小王" | memory.md 用户信息 | 跟人有关 |
| 这个项目在 E 盘 | memory.md 项目上下文 | 跟环境有关 |
| 改东西前先问我 | memory.md 通用经验 | 软性行为规则 |
| "今天天气不错" | 不记 | 无关信息 |

---

## 规则二：写入规则 — 什么时候写？

### 2.1 Flow Card 写入

```
触发条件: LLM 返回纯文本 + all_steps_out 有内容
  即: 一次完整的带 tool call 的对话回合结束

写入内容:
  1. Card 本体: record_successful_flow() 自动完成
     - flow_hash / steps / intent_summary / intent_vector
  2. 踩坑检测: enrich_card_with_experience()
     - 规则检测: 同一步骤失败后重试成功 → 记 pitfall
     - 规则检测: 步骤返回 error 但后续有 workaround → 记 pitfall
     - LLM 提取: 后续 batch 处理，对多次执行的流程总结经验

执行位置: agent.py L327-330 record_successful_flow() 调用处
```

### 2.2 Flow Graph 写入

```
触发条件: 同 Flow Card

写入内容:
  update_graph_from_steps()
  - (method_A, path_A) → (method_B, path_B) 计数 +1
  - 只记相邻步骤的转移关系

执行位置: agent.py L331 update_graph_from_steps() 调用处
```

### 2.3 memory.md 写入 — Hook 规则驱动

```
触发条件: LLM 返回纯文本后（同 Flow Card 时机）

规则提取（不依赖 LLM）:
  规则 1: reply 包含「失败」「报错」「超时」「错误」等关键词
          + 后续有重试成功 → 记「踩坑记录」
  规则 2: terminal 输出包含路径信息 (dir / pwd / where)
          + 看起来是项目目录 → 记「项目上下文」
  规则 3: 用户明确说了偏好 ("我喜欢…" / "叫我…" / "不要…")
          → 记「用户信息」

执行位置: agent.py, 在 record_successful_flow() 之后
          调用 hooks.on_memory_extract()
```

### 2.4 memory.md 写入 — LLM 自主驱动

```
触发条件: Agent 自主调用 memory_write tool

适用场景:
  - 用户明确说「记住这个」
  - Agent 发现值得记的信息
  - 合并/清理 memory.md 中的旧条目

执行位置: builtins.py memory_write handler
```

---

## 规则三：读取/注入规则 — 什么时候喂给 LLM？

### 3.1 memory.md 注入

```
触发条件: 每次组装 system prompt 时

注入方式: 作为 system prompt 的一部分
  「以下是你之前记住的信息：」
  [memory.md 全文]

注意: 每次对话开始注入，不是每次 LLM 调用都注入
      由 assemble_sys_prompt() 处理
```

### 3.2 Flow Card 检索注入

```
触发条件: 每次构建 messages 时 (build_agent_messages_with_history)

注入方式: 作为独立的 system message
  「【历史相似流程】
   1. xxx
      步骤: ...
      踩坑提醒: ...
      经验: ...」

检索策略:
  - 默认 top_k=2
  - 相似度阈值 0.4 (由 MEMORY_MIN_SIM 控制)
  - MMR 多样性重排，避免返回的同质化流程
```

---

## 规则四：生命周期规则 — 怎么维护？

### 4.1 Flow Card 生命周期

| 事件 | 行为 |
|---|---|
| 首次执行某流程 | 新建 Card |
| 再次执行相同流程 | 合并：更新 intent_examples + intent_vector |
| 用户反馈 approved | approved_count +1 |
| 用户反馈 rejected | rejected_count +1 |
| triggered_count >= 3 + approved=0 + rejected >= 3 | 删除 Card |
| 执行频率过低（低于 median*0.1） | 尾淘汰（已有）|

### 4.2 memory.md 生命周期

| 事件 | 行为 |
|---|---|
| 首次有内容 | 创建 memory.md |
| 追加新信息 | memory_write(action=append) |
| 信息过时 | memory_write(action=update) |
| 信息错误 | memory_write(action=delete) |
| 文件超过 200 行 | Agent 主动合并同类项 |
| 条目冲突 | 以最新的为准 |

### 4.3 冲突解决

```
memory.md 内冲突:
  - 同 section 同 key → 以最新写入为准
  - Agent 发现矛盾时 → 用 ask_user 确认后更新

Flow Card 内冲突:
  - 同 flow_hash → 合并，合并 intent_examples
  - 同 pitfall 重复 → set 去重
  - 新旧经验矛盾 → 以 recent 够数（success_count>=3）为准

跨系统冲突（memory.md 说 A，Flow Card 暗示 B）:
  - Flow Card 优先（因为跟具体流程绑定，更精确）
  - memory.md 的通用建议覆盖流程例外
```

---

## 规则五：优先级 — 多条记忆冲突时听谁的？

```
高优先级 ↑
          user 明确指令（"别用 POST，用 GET"）
          Flow Card 的踩坑提醒（"步骤2容易超时"）
          Flow Card 的经验总结（"标准流程是 A→B→C"）
          memory.md 项目上下文（"Capture 在 port 8000"）
          memory.md 用户偏好（"喜欢简洁"）
低优先级 ↓
```

---

## 规则六：禁止操作

```
禁止:
  - 在 memory.md 里记敏感信息（密码、token）
  - 自动删除用户明确写下的条目
  - 在没有用户确认的情况下覆盖 Flow Card
  - 单次写入超过 50 行（防止刷爆上下文）
```

---

## 框架总览

```
                    ┌─────────────────────────────┐
                    │     规则一：分类             │
                    │  这条信息该进哪套记忆？       │
                    └──────────┬──────────────────┘
                               │
                    ┌──────────v──────────────────┐
                    │     规则二：写入             │
                    │  Hook 规则 → memory.md       │
                    │  LLM tool  → memory.md       │
                    │  自动写入   → Flow Card      │
                    │  自动写入   → Flow Graph     │
                    └──────────┬──────────────────┘
                               │
                    ┌──────────v──────────────────┐
                    │     规则三：读取             │
                    │  system prompt → memory.md   │
                    │  system message → Flow Card  │
                    └──────────┬──────────────────┘
                               │
                    ┌──────────v──────────────────┐
                    │     规则四：维护             │
                    │  去重 / 尾淘汰 / 冲突解决    │
                    └─────────────────────────────┘
```
