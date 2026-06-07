# NanoGhost 飞书渠道输出链路全景

## 一、总链路图

```
AgentExecutor 决策循环
    │
    ▼  [Phase 1] 事件流 (executor.py)
yield 各类事件给 Presenter
    │
    ▼  [Phase 2] Presenter 渲染 (presenter.py)
将事件翻译为渠道消息，调用 ChannelIO
    │
    ▼  [Phase 3] FeishuIO 处理 (io.py)
@提及替换 → 选择消息格式 → 调用 API
    │
    ▼  [Phase 4] Feishu API 发送 (api.py)
REST API 调用 → 飞书消息
    │
    ▼  [Phase 5] 回合后处理 (postprocess.py)
Card + Graph + memory.md 写入（不阻塞回复）
```

---

## 二、Phase 1：事件流 (executor.py)

**位置**：`src/agent_core/engine/executor.py:98-358`

AgentExecutor.run() 是一个异步生成器，按顺序 yield 事件。

### 事件类型总表

| 事件类型 | 触发时机 | event_data | Presenter 处理 |
|----------|----------|------------|----------------|
| `session` | 决策循环前 | `{"session_id": "..."}` | 当前不处理 |
| `status` | 每轮开始 | `{"message": "思考中…"}` | 当前忽略（不发送到飞书） |
| `images_stored` | 图片入库后 | `{"image_ids": [...]}` | 当前不处理 |
| `text_stream` | LLM 返回文本时 | `{"content": "..."}` | feedback_level >= 2 时缓存/发送到飞书 |
| `tool_call` | LLM 请求调用工具 | `{"name": "...", "preview": "...", "id": "..."}` | feedback_level >= 3 时发送 emoji + 名称到飞书 |
| `tool_result` | 工具执行完毕 | `{"name": "...", "ok": bool, "summary": "..."}` | feedback_level >= 4 时发送摘要到飞书 |
| `step_start` | 子工具主动 yield | `{"step": N, "method": "...", "path": "..."}` | 当前不处理 |
| `step_done` | 子工具完成 | `{"step": N, "ok": bool, "path": "...", "result": {...}}` | 提取 images 缓存 |
| `skill_loaded` | 技能加载完成 | `{"name": "..."}` | 当前不处理 |
| `subagent_start` | 子代理启动 | `{"name": "...", "type": "...", "description": "..."}` | 当前不处理 |
| `subagent_text` | 子代理输出文本 | `{"name": "...", "content": "..."}` | 当前不处理 |
| `subagent_result` | 子代理返回 | `{"name": "...", "reply": "..."}` | 当前不处理 |
| `ask_user` | 询问用户 | `{"question": "...", "options": [...], "session_id": "..."}` | 格式化后返回（不发送到飞书） |
| `error` | LLM/工具异常 | `{"error": "..."}` | 截断回复为 error 文本 |
| `done` | 正常结束 | `{"ok": true, "reply": "...", "session_id": "...", "steps": [...]}` | 发送最终回复到飞书 |

### 决策循环流程

```
for _round in range(120):
    yield ("status", ...)

    # 调用 LLM: llm.chat(messages, tools=tools_schemas)

    分支 A: LLM 返回纯文本（无 tool_call）
        yield ("text_stream", ...)
        yield ("done", {"reply": "...", "steps": [...]})
        break（循环结束）

    分支 B: LLM 返回 tool_calls
        yield ("text_stream", ...)  # 思考过程（可选）
        for tc in tool_calls:
            yield ("tool_call", ...)
            result = tool_registry.dispatch(tc)
            yield ("tool_result", ...)
            messages.append(tool role msg)
        continue（下一轮继续）

    分支 C: LLM 返回空
        yield ("error", ...)
        break

120 轮耗尽 →
    yield ("done", {"reply": "已执行完成。"})
```

---

## 三、Phase 2：Presenter 渲染 (presenter.py)

**位置**：`src/agent_core/presenter.py:58-210`

**职责**：把事件流翻译成渠道消息。

### feedback_level 控制

| level | 飞书端表现 |
|-------|-----------|
| 1 | 只接收最终回复 |
| 2 | + 流式文本（text_stream） |
| 3 | + 工具调用提示（emoji + 名称 + 参数预览） |
| 4 | + 工具执行结果摘要 |

在 `<INSTANCE_DIR>/config.yaml` 中配置：
```yaml
feedback_level: 3
```

### 各部分处理逻辑

```
收到 session 事件 → 忽略

收到 status 事件 → 忽略（不发送到飞书）

收到 text_stream 事件（feedback >= 2）:
    text_stream_content = 累加文本内容

收到 tool_call 事件（feedback >= 3）:
    如果有缓存的 text_stream，先发出去
    发送: "🔍 搜索: 北京天气"

收到 tool_result 事件（feedback >= 4）:
    发送: "  查到北京晴25°C"

收到 step_done 事件:
    提取出图的 base64 图片，缓存到 out_images

收到 ask_user 事件:
    _format_ask_user_text() 格式化问题+选项
    break 停止循环，不发送到飞书

收到 error 事件:
    reply_text = "(Agent error: ...)"
    break

收到 done 事件:
    回复:
      如果有 message_id → io.reply(message_id, reply_text)   （回复指定消息）
      否则 → io.send_text(chat_id, reply_text)               （新发送）
    提取 reply_text 中的 img-xxx 引用 → 转为 base64 → 缓存到 out_images
    done_sent = True
    break
```

### 兜底发送

```
如果没有收到 done 事件（中途 break）且 reply_text 非空:
    io.reply / io.send_text

如果有 out_images:
    io.send_images(chat_id, out_images[:10])
```

### 最终删除 Reaction

```
finally:
    if message_id and reaction_id:
        io.delete_reaction(message_id, reaction_id)
```

---

## 四、Phase 3：FeishuIO 处理 (io.py)

**位置**：`src/agent_core/channel/feishu/io.py:19-62`

**职责**：飞书 ChannelIO 实现。发送前处理 @提及，选择消息格式。

### 处理链

```
text (来自 Presenter)
    │
    ▼  _replace_at_mentions()
把 @昵称 替换为 <at user_id="ou_xxx">@昵称</at>
    │
    ▼  选择发送方式
```

### @提及替换

```python
def _replace_at_mentions(self, text: str) -> str:
    for name, uid in sorted(self._name_map.items(), key=lambda x: -len(x[0])):
        text = text.replace(f"@{name}", f'<at user_id="{uid}">@{name}</at>')
    return text
```

**行为**：
- 按名字长度降序替换：先匹配 `@李逍遥`，再匹配 `@李`（避免短名匹配长名的一部分）
- 如果 name_map 里没有这个昵称，保持纯文本 `@昵称` 不变

### 消息路由

| FeishuIO 方法 | Presenter 调用位置 | 逻辑 |
|---|---|---|
| `send_text(chat_id, text)` | tool_call 时、兜底发送时 | @替换后 → `api.send_text_message_to_chat()` |
| `reply(message_id, text)` | done 事件回复时 | @替换后 → 判断是否有 `<at>` → 有则走纯文本，否则走 interactive 卡片 |
| `send_images(chat_id, b64_list)` | step_done / done 收集的图片 | → `api.send_images_base64_to_chat()` |
| `add_reaction(message_id)` | turn 开始时 | → `api.add_reaction_to_message()` |
| `delete_reaction(message_id, reaction_id)` | turn 结束时 | → `api.delete_reaction_to_message()` |

### reply 的降级逻辑

```
reply("好的 @张三 这就处理")
    │
    ▼ _replace_at_mentions()
"好的 <at user_id="ou_xxx">@张三</at> 这就处理"
    │
    ▼ 检测是否有 <at user_id=
有 → api.reply_text_to_message()   （纯文本消息，支持 @）
无 → api.reply_to_message()        （interactive 卡片，支持 markdown，不支持 @）
```

**为什么降级**：飞书 interactive 卡片的 markdown 元素不支持 `<at>` 标签，如果卡片里有 `<at>`，会显示为纯文本。

### send_text 的格式

send_text 一律使用 `send_text_message_to_chat`（纯文本消息），纯文本天然支持 `<at>` 标签。

---

## 五、Phase 4：飞书 API 发送 (api.py)

**位置**：`src/agent_core/channel/feishu/api.py:122-261`

### 消息发送方法总表

| 方法 | msg_type | Content 格式 | 用途 |
|------|----------|-------------|------|
| `send_text_message_to_chat` | `text` | `{"text": "..."}` | 独立的纯文本消息 |
| `send_markdown_message_to_chat` | `interactive` | 卡片 JSON | 独立的 markdown 消息（失败降级为 text） |
| `reply_to_message` | `interactive` | 卡片 JSON | 回复指定消息（失败降级为 text） |
| `reply_text_to_message` | `text` | `{"text": "..."}` | 回复指定消息（纯文本） |
| `send_images_base64_to_chat` | `image` | `{"image_type": "...", "image": "base64"}` | 发送图片 |

### 纯文本消息（支持 @）

POST `/im/v1/messages?receive_id_type=chat_id`

```json
{
  "receive_id": "oc_xxx",
  "msg_type": "text",
  "content": "{\"text\":\"好的 <at user_id=\\\"ou_xxx\\\">@张三</at> 这就处理\"}"
}
```

### interactive 卡片消息（支持 markdown，不支持 @）

POST `/im/v1/messages?receive_id_type=chat_id`

```json
{
  "receive_id": "oc_xxx",
  "msg_type": "interactive",
  "content": "{\"config\":{\"wide_screen_mode\":true},\"elements\":[{\"tag\":\"markdown\",\"content\":\"好的 **张三** 这就处理\"}]}"
}
```

### 图片消息

POST `/im/v1/messages?receive_id_type=chat_id`

```json
{
  "receive_id": "oc_xxx",
  "msg_type": "image",
  "content": "{\"image_type\":\"png\",\"image\":\"base64data...\"}"
}
```

### Reaction

| 方法 | API 路径 | 用途 |
|------|---------|------|
| `add_reaction_to_message` | POST `/im/v1/messages/{message_id}/reactions` | 添加处理中的 reaction（默认 SKULL） |
| `delete_reaction_to_message` | DELETE `/im/v1/messages/{message_id}/reactions/{reaction_id}` | 删除处理中的 reaction |

### Token 管理

```python
class FeishuTokenManager:
    # 缓存 tenant_access_token
    # 自动刷新（expire_at - 60s 时为阈值）
    # 请求 99991663/99991664 错误时强制刷新重试
```

---

## 六、Phase 5：回合后处理 (postprocess.py)

**位置**：`src/agent_core/memory/postprocess.py:15-73`

**执行时机**：`done` 事件 yield 后，通过 `asyncio.create_task` 异步执行，**不阻塞用户回复**。

```
Phase 1: 写入 Card (record_successful_flow)
  → 从 steps 生成 flow_hash
  → 写入/合并到 agent_memory_cards 表

Phase 2: 写入 Graph (update_graph_ml)
  → 相邻步骤配对 → classify() → OpCode
  → 写入 agent_edges_ml 表 (L1~L4)

Phase 3: LLM 总结 experience (enrich_card_experience)
  → 输入: intent_summary + steps_summary + reply
  → LLM 生成经验文本
  → 追加到 card.experience_notes

Phase 4: 写入 memory.md
  → extract_memory_md_entries() 从对话中提取关键信息
  → append_to_memory_md() 追加到 <INSTANCE_DIR>/memory.md
```

---

## 七、完整输出示例

### 场景

飞书群聊，用户说 `@闲小淘 帮我查一下北京的天气`。

### 飞书端用户看到的内容

**Step 1**：bot 左上角闪烁 skull reaction → 表示正在处理

**Step 2**（feedback_level >= 3）：
```
🔄 搜索: 北京天气
　✓ 查到北京晴 25°C
```

**Step 3**（最终回复）：
```
查到北京今天天气是 ☀️ 晴，25°C，东南风 3 级。适合出门活动。
```

**Step 4**：skull reaction 消失

---

## 八、各模块职责速查表（输出侧）

| 模块 | 文件 | 职责 | 输入 | 输出 |
|------|------|------|------|------|
| **执行引擎** | `executor.py:98` | 120 轮决策循环，yield 事件流 | (user_message, config, session_id) | 事件序列 (session, status, text_stream, tool_call, tool_result, done) |
| **Presenter** | `presenter.py:58` | 事件流 → 渠道消息 | 事件序列 | ChannelIO 调用 |
| **FeishuIO** | `io.py:19` | @替换 + 消息路由 | (chat_id, text, message_id) | api.py 调用 |
| **Feishu API** | `api.py:122` | REST API 封装 | (chat_id, message_id, content) | 飞书 HTTP 请求 |
| **Token 管理** | `api.py:36` | tenant_access_token 缓存刷新 | (app_id, app_secret) | token 字符串 |
| **记忆写入** | `postprocess.py:15` | 回合后 Card + Graph + memory.md | (agent, steps, reply) | DB 写入 & 文件写入 |
