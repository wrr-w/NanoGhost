# NanoGhost 飞书渠道输入链路全景

## 一、总链路图

```
飞书 SDK 事件流
    │
    ▼  [Phase 0] convert_sdk_event_to_dict (turn.py)
lark_oapi 对象 → 普通 dict
    │
    ▼  [Phase 1] parse_event (turn.py)
dict → MessageSource + MessageContext
    │
    ▼  [Phase 2] 缓存 mention 映射 (ws_client.py)
name → user_id → SessionStore.mention_name_map (内存 + DB)
    │
    ▼  [Phase 3] ContextBuilder 格式化 (message_context.py)
3a: build_user_message()   → user_text
3b: build_session_context()  → 会话上下文块（注入 system prompt）
    │
    ▼  [Phase 4] Presenter 编排 (presenter.py)
4a: BotInstance.refresh_memory()  → 注入 memory.md
4b: 拼接完整 sys_prompt = base_sys_prompt + "\n\n" + session_context
4c: 构建 AgentConfig
    │
    ▼  [Phase 5] build_agent_messages_with_history (messages.py)
5a: 注入 system prompt（聚合基座 sys_prompt）
5b: 注入记忆召回【历史相似流程】（可选）
5c: 从 DB 加载会话历史（按 root_id 过滤）
     → 先按条数截断（最多 200 条）
     → 再按 Token 截断（最多 200K）
5d: 追加当前用户输入（图片 + 文本）
    │
    ▼  [Phase 6] 注入 SKILL.md 索引 (executor.py)
追加 role:system 到 messages
    │
    ▼  [Phase 7] LLM.chat(messages)
(messages + tools_schemas)
```

---

## 二、Phase 0：飞书 SDK 原始事件 (convert_sdk_event_to_dict)

**位置**：`src/agent_core/channel/feishu/turn.py:240-292`

**职责**：把 `lark_oapi` SDK 事件对象转成普通 Python dict。

### 解析的字段 ✓

| 字段 | 来源 | 用途 |
|------|------|------|
| `chat_id` | `msg.chat_id` | Session 标识 |
| `chat_type` | `msg.chat_type` | group / p2p |
| `message_id` | `msg.message_id` | 消息唯一 ID，去重、回复 |
| `message_type` | `msg.message_type` | text / image / file / ... |
| `content` | `msg.content` | JSON 字符串，含文本或图片 key |
| `parent_id` | `msg.parent_id` | 被回复消息的 ID |
| `root_id` | `msg.root_id` | 话题根消息 ID |
| `mentions[].key` | `m.key` | 占位符 `@_user_N` |
| `mentions[].name` | `m.name` | 被 @ 的用户显示名 |
| `mentions[].id.open_id` | `m.id.open_id` | 被 @ 用户的 open_id |
| `sender.sender_id.open_id` | `sd.sender_id.open_id` | 发送者 open_id |
| `sender.sender_type` | `sd.sender_type` | user / bot |

### 未解析/丢弃的字段 ✗

| 字段 | 说明 |
|------|------|
| `header.event_id` | 事件全局 ID，可用于幂等 |
| `header.app_id` | 哪个应用收到的 |
| `header.create_time` | 事件创建时间戳 |
| `event.message.mentions[].tenant_key` | 用户所在租户 |
| `event.sender.tenant_key` | 发送者租户 |
| `event.sender.sender_id.union_id` | 跨应用统一 ID（已提取但未使用） |
| `event.sender.sender_id.user_id` | 企业微信/飞书后台 user_id（部分场景） |
| `event.message.body`（完整原始 JSON） | 仅提取 text，其他丢弃 |

---

## 三、Phase 1：turn.py 解析 (parse_event)

**位置**：`src/agent_core/channel/feishu/turn.py:47-134`

**职责**：把飞书事件 dict 翻译成平台无关的 `(MessageSource, MessageContext)`。

### 1a. 发送者名称解析（三段式兜底）

```
1. sender.name（SDK 的 sender 字段通常不含 name）→ 空
2. API 查 get_user_name(open_id) → 查到就用，查不到 None
3. 从 mentions 列表里捞（谁的 open_id 匹配 sender 就取谁的名字）
4. 全部失败 → "用户"
```

### 1b. 回复原文拉取

```python
if parent_id:
    reply_msg = get_message_by_id(parent_id)
    reply_to_text = parse_message_content(reply_msg.content, reply_msg.msg_type)
```

只拉取**直接父消息**的文本，不拉整棵话题树。

### 1c. 内容提取

| message_type | 行为 |
|---|---|
| `text` | 解析 content JSON 取 `.text` |
| `image` | 提取 image_key 到列表，text 为空（图片后续由 ws_client 下载） |
| `file` | 下载文件 → 文本文件解码为内容，二进制文件只返回文件名和大小 |
| `post`（富文本） | 走 `extract_text_from_event_message` 默认解析 |
| `interactive`（卡片） | 同上，只提取最简文本 |

### MessageSource 输出格式

```python
MessageSource(
    platform="feishu",
    sender_id="ou_xxx",          # 发送者 open_id
    sender_name="张三",           # 显示名（三段式兜底后的值）
    chat_id="oc_xxx",            # 群/私聊 ID
    chat_name=chat_id,           # 当前 = chat_id（未查群名）
    chat_type="group" / "p2p",
    thread_id="",                # 已提取但当前无任何地方使用
)
```

### MessageContext 输出格式

```python
MessageContext(
    text="@_user_1 帮我查一下天气",   # 原始文本，@占位符尚未替换
    message_type="text",
    message_id="om_xxx",
    parent_id="om_yyy",            # 回复引用（可能为空）
    root_id="om_zzz",              # 话题根 ID（可能为空）
    reply_to_text="昨天那个需求已经改完了",  # 父消息文本内容
    mentions=[
        MentionRef(key="@_user_1", name="张三", user_id="ou_xxx", is_bot=False),
        MentionRef(key="@_user_2", name="李四", user_id="ou_yyy", is_bot=False),
        MentionRef(key="@_user_3", name="小助手", user_id="ou_bot", is_bot=True),
    ],
    transcribed_text="",           # 语音转文字（当前未用，预留）
)
```

---

## 四、Phase 2：缓存 mention 映射 (ws_client)

**位置**：`src/agent_core/channel/feishu/ws_client.py:207-210`

把当前消息中出现的所有人的 name → user_id 记到 session。

```python
# 记录发送者自身
sessions.record_mention(chat_id, source.sender_name, source.sender_id)

# 记录消息中 @ 的所有人
for m in ctx.mentions:
    if m.name and m.user_id:
        sessions.record_mention(chat_id, m.name, m.user_id)
```

**存储**：内存 `SessionStore.mention_name_map` + 数据库 `agent_chat_mentions` 表。

**用途**：FeishuIO 发送消息时，把 `@昵称` 替换为飞书识别的 `<at user_id="ou_xxx">@昵称</at>`。

---

## 五、Phase 3：ContextBuilder 格式化

**位置**：`src/agent_core/channel/message_context.py`

### 3a. build_user_message — 当前用户文本

**方法**：`message_context.py:198-234`

**处理链**（顺序固定）：

```
原始文本: "@_user_1 帮我查一下北京的天气 谢谢 @_user_2"

Step 1: @_user_N 占位符 → @姓名
        "@张三 帮我查一下北京的天气 谢谢 @李四"

Step 2: 剥离 bot 自 @mention（开头/结尾的 @bot 名去掉）
        "帮我查一下北京的天气 谢谢 @李四"

Step 3: 非 bot 的 @提及 → [提及了: 姓名]
        "[提及了: 张三, 李四]

帮我查一下北京的天气 谢谢 @李四"

Step 4: 回复引用 → [回复给: "原文"]
        如有 parent_id + reply_to_text:
        "[回复给: "昨天那个需求已经改完了"]

[提及了: 张三, 李四]

帮我查一下北京的天气 谢谢 @李四"

Step 5: 发送者前缀 [姓名]
        "[张三] [回复给: "昨天那个需求已经改完了"]

[提及了: 张三, 李四]

帮我查一下北京的天气 谢谢 @李四"

Step 6: 语音转文字（当前为空，跳过）
```

### 3b. build_session_context — 会话上下文块

**方法**：`message_context.py:176-197`

每次 session 首次使用或 chat_id 变更时，注入 system prompt。内容：

**群聊：**
```
## 当前会话上下文

**来源:** Feishu (群聊: oc_xxxxx)
**当前身份:** 闲小淘
**会话类型:** 多人会话——每条消息前面会标注发送者姓名。
**连接的平台:** local, feishu: 已连接 ✓
```

**私聊：**
```
## 当前会话上下文

**来源:** Feishu (私聊: oc_xxxxx)
**当前身份:** 闲小淘
**用户:** 张三
**连接的平台:** local, feishu: 已连接 ✓
```

---

## 六、Phase 4：Presenter 编排

**位置**：`src/agent_core/presenter.py:91-110`

### 6a. 刷新记忆 (BotInstance.refresh_memory)

**位置**：`src/agent_core/channel/instance.py:37-57`

从 `<INSTANCE_DIR>/memory.md` 读取内容，注入到 `_base_sys_prompt`。

```python
# 注入格式
self._base_sys_prompt += "\n\n## 记住的信息\n\n" + memory_content + "\n\n"
```

如果已有 `## 记住的信息` 段，先删除旧段再追加。

### 6b. 拼接完整 system prompt

```python
full_sys_prompt = identity.get_base_sys_prompt()   # = agent_profile.md + agent_rules_conduct.md + memory.md 注入
                + "\n\n" + session_context          # = build_session_context() 的结果
```

注入到 `AgentConfig.sys_prompt`。

### 6c. 构建 AgentConfig

```python
AgentConfig(
    base_url=...,
    sys_prompt=full_sys_prompt,     # 聚合后的完整 system prompt
    api_spec={},
    history_max_messages=200,
    history_max_tokens=200_000,
    root_id=ctx.root_id,            # 话题 ID（用于过滤历史消息）
)
```

---

## 七、Phase 5：build_agent_messages_with_history

**位置**：`src/agent_core/engine/messages.py:71-213`

### 完整 messages 数组结构

```
messages = [
    # ── [0] system prompt（聚合基座 + 会话上下文） ──
    {
        "role": "system",
        "content": [{"type": "text", "text": "<完整的 system prompt>"}]
    },

    # ── [1] 记忆召回（可选，无匹配则跳过） ──
    {
        "role": "system",
        "content": [{
            "type": "text",
            "text": "【历史相似流程】\n"
                    "1. 创建任务\n"
                    "   步骤: GET /api/tasks -> POST /api/tasks\n"
                    "   经验: 先查后建避免重复\n\n"
                    "可参考这些流程。注意踩坑提醒。"
        }]
    },

    # ── [2..N] 会话历史（从 DB 加载，按 root_id 过滤） ──

    # 历史 user 文本消息
    {"role": "user", "content": [{"type": "text", "text": "[张三] 帮我查天气"}]},

    # 历史 user 图片消息
    {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
            {"type": "text", "text": "_image_reference: img-xxx"}
        ]
    },

    # 历史 assistant 纯文本回复
    {"role": "assistant", "content": [{"type": "text", "text": "好的，我查一下。"}]},

    # 历史 assistant + tool_calls（有执行摘要）
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "帮我查一下天气..."},
            {"type": "text", "text": "\n\n【执行摘要】\n步骤1 web_search 北京天气: 查到晴天25°C..."}
        ],
        "reasoning_content": "<DeepSeek 思考链>"
    },

    # 历史 tool 返回结果
    {"role": "tool", "tool_call_id": "call_xxx", "content": "北京天气: 晴, 25°C"},

    # ── [N+1] 当前用户输入（最后一条） ──
    {
        "role": "user",
        "content": [
            # 图片（如有）
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
            {"type": "text", "text": "_image_reference: img-xxx"},
            # 文本（经过 ContextBuilder 格式化）
            {"type": "text", "text": "[张三] [回复给: \"昨天那个需求已经改完了\"]

[提及了: 李四]

@李四 一起看一下"}
        ]
    }
]
```

### 历史截断算法

```
Phase 1: 按条数
    db.get_agent_messages(session_id, root_id=root_id)
    如果 > 200 条，只保留最近的 200 条

Phase 2: 按 Token 估算
    _truncate_history_by_tokens()
    每条文本 ≈ len(text)/3 token，图片固定 ≈ 1000 token
    从最旧的消息开始丢弃，直到总 token < 200K
```

当前用户的输入（最后一条）**不参与截断**。

---

## 八、Phase 6：注入 SKILL.md 索引 (executor)

**位置**：`src/agent_core/engine/executor.py:157-165`

在 messages 数组末尾追加一条 system 消息：

```python
messages.append({
    "role": "system",
    "content": [{"type": "text", "text": skill_block}],
})
```

`skill_block` 的内容是所有已发现技能的列表（名称 + 描述），由 `SkillRegistry.build_skill_context()` 生成。格式示例：

```
## 可用技能

- lark-calendar: 飞书日历管理
- lark-im: 飞书即时通讯
```

---

## 九、Phase 7：最终 LLM 调用

**位置**：`src/agent_core/engine/executor.py:214`

```python
response = llm.chat(
    messages=messages,           # 完整的 messages 数组
    temperature=0.1,
    tools=tools_schemas,         # ToolRegistry 中所有注册工具的函数定义
)
```

---

## 十、完整示例（群聊回复话题）

### 场景

飞书产品群，张三在话题串"需求评审"下回复李四：
```
@闲小淘 帮我查一下北京的天气 谢谢 @李四
```

### 最终给 LLM 的 messages 展开

```
────────────────────────────────────────
messages[0] — system prompt
────────────────────────────────────────
你是一个智能助手...（agent_profile.md）

行为规则...（agent_rules_conduct.md）

## 记住的信息
（memory.md 内容）

## 当前会话上下文

**来源:** Feishu (群聊: oc_xxxxx)
**当前身份:** 闲小淘
**会话类型:** 多人会话——每条消息前面会标注发送者姓名。
**连接的平台:** local, feishu: 已连接 ✓
────────────────────────────────────────
messages[1] — 记忆召回（可选）
────────────────────────────────────────
【历史相似流程】
1. 查天气
   步骤: web_search 天气 -> 返回结果
   经验: 先确认城市再搜索
────────────────────────────────────────
messages[2..4] — 话题历史（同 root_id 的 3 条历史）
────────────────────────────────────────
{"role": "user",     "content": "[{type: text, text: \"[张三] 明天的需求评审谁讲？\"}]"}
{"role": "assistant","content": "[{type: text, text: \"我来确认一下参会人。\"}]"}
{"role": "user",     "content": "[{type: text, text: \"[李四] 我来主讲吧\"}]"}
────────────────────────────────────────
messages[5] — 当前用户输入
────────────────────────────────────────
{"role": "user",     "content": [
    {type: text, text: "[张三] [回复给: \"明天的需求评审谁讲？\"]

[提及了: 李四]

@李四 一起查看一下帮我查一下北京的天气"}
]}
────────────────────────────────────────
messages[6] — SKILL.md 索引
────────────────────────────────────────
{"role": "system",   "content": [{type: text, text: "## 可用技能\n\n- lark-calendar: 飞书日历..."}]}
```

---

## 十一、各模块职责速查表

| 模块 | 文件 | 职责 | 输入 | 输出 |
|------|------|------|------|------|
| **SDK 转换** | `turn.py:240` | lark_oapi 对象 → 普通 dict | SDK Event 对象 | Dict |
| **事件解析** | `turn.py:47` | 飞书 dict → 平台无关 (MessageSource, MessageContext) | 飞书事件 dict | (MessageSource, MessageContext) |
| **mention 缓存** | `ws_client.py:207` | 缓存 name→user_id 到 SessionStore（内存+DB） | source, ctx | 更新 mention_name_map |
| **用户文本格式化** | `message_context.py:198` | 消息格式化为 LLM 可读文本 | (MessageSource, MessageContext) | user_text |
| **会话上下文** | `message_context.py:176` | 生成当前 session 的场景描述 | MessageSource | 多行文本块 |
| **记忆刷新** | `instance.py:37` | 把 memory.md 注入 sys_prompt | INSTANCE_DIR | 更新 _base_sys_prompt |
| **Presenter 编排** | `presenter.py:58` | 一轮对话的完整生命周期编排 | (agent, source, ctx, user_text) | 发送回复，管理事件流 |
| **消息拼装** | `messages.py:71` | 拼装完整 messages 数组 | (sys_prompt, user_message, session_id, ...) | messages 数组 |
| **执行引擎** | `executor.py:98` | 120 轮 tool-calling 决策循环 | (user_message, config, ...) | 事件流 |
| **历史截断** | `messages.py` | 按条数+Token 双重截断 | agent_messages 列表 | 裁剪后的列表 |
| **记忆写入** | `postprocess.py:15` | 回合后写入 Card + Graph + memory.md | (agent, steps, reply) | 更新 DB & 文件 |
| **飞书 API** | `api.py` | 飞书 REST API 封装 | (chat_id, message_id, ...) | API 响应 |
