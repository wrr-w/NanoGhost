# NanoGhost 全面改造清单

> 涵盖所有已改和待改的内容，按文件组织，每处都有完整代码

---

## 目录

1. [已完成的修复](#已完成)
2. [记忆系统代码改造](#记忆代码)
3. [提示词更新](#提示词)
4. [配置文件更新](#配置)
5. [实施顺序](#实施)

---

<a name="已完成"></a>
## 一、已完成的修复

### 1.1 DeepSeek reasoning_content 修复

涉及 5 个文件，解决 DeepSeek thinking mode 的 400 错误。

#### interfaces/llm.py — LLMResponse 新增字段

```python
@dataclass
class LLMResponse:
    content: Optional[str] = None
    reasoning_content: Optional[str] = None  # <-- 新增
    tool_calls: Optional[List[Any]] = None
```

#### adapters/llm.py — chat() 捕获 reasoning_content

```python
def chat(self, messages, temperature=0.1, tools=None):
    kwargs = dict(model=self.model, messages=messages, temperature=temperature)
    if tools:
        kwargs["tools"] = tools
    response = self.client.chat.completions.create(**kwargs)
    choice = response.choices[0]
    content = choice.message.content
    reasoning_content = getattr(choice.message, "reasoning_content", None)  # <--
    tool_calls = None
    if choice.message.tool_calls:
        tool_calls = [ToolCall.from_openai(tc) for tc in choice.message.tool_calls]
    return LLMResponse(content=content, reasoning_content=reasoning_content, tool_calls=tool_calls)
```

#### adapters/database.py — 表加列 + add_agent_message 支持

```python
# CREATE TABLE 新增 reasoning_content TEXT 列
# add_agent_message 新增 reasoning_content=None 参数
# _init_db() 末尾有 ALTER TABLE migration

def add_agent_message(self, session_id, role, content, type="text", steps_json=None, reasoning_content=None):
    conn.execute(
        "INSERT INTO agent_messages (...) VALUES (?,?,?,?,?,?,?,?)",
        (mid, session_id, role, type, content, steps_json, reasoning_content, now),
    )
```

#### agent.py — 保存 assistant 消息时传 reasoning

```python
# 文本回复
self.db.add_agent_message(session_id, "assistant", reply, reasoning_content=response.reasoning_content)

# tool call 消息
assistant_msg = {
    "role": "assistant",
    "content": response.content,
    "reasoning_content": response.reasoning_content,  # <--
    "tool_calls": [...],
}
self.db.add_agent_message(session_id, "assistant", json.dumps(assistant_msg), reasoning_content=response.reasoning_content)
```

#### engine/messages.py — 历史重构带上 reasoning

```python
# 构建 assistant 消息时
assistant_msg = {"role": "assistant", "content": assistant_content}
if reasoning_content:
    assistant_msg["reasoning_content"] = reasoning_content
```

---

<a name="记忆代码"></a>
## 二、记忆系统代码改造

### 2.1 memory/models.py — Card 加字段

```python
@dataclass
class AgentMemoryCard:
    # 原有字段
    id: str
    flow_hash: str
    intent_summary: str
    intent_examples: List[str]
    intent_vector: List[float]
    flow_signature: Dict[str, Any]
    steps: List[Dict[str, Any]]
    success_count: int
    total_rounds: int
    created_at: float
    updated_at: float
    approved_count: int = 0
    rejected_count: int = 0
    trigger_count: int = 0
    scene_tag: Optional[str] = None
    namespace: Optional[str] = None

    # 新增字段
    pitfalls: List[str] = field(default_factory=list)
    """踩坑记录。LLM 在步骤失败/重试时生成。"""
    experience_notes: List[str] = field(default_factory=list)
    """经验总结。LLM 在累计执行 5 次时生成。"""
```

同时更新 `from_dict()` 和 `to_dict()`。

### 2.2 adapters/database.py — 表 migration

```python
# _init_db() 末尾新增
try:
    conn.execute("ALTER TABLE agent_memory_cards ADD COLUMN pitfalls TEXT DEFAULT '[]'")
except Exception:
    pass
try:
    conn.execute("ALTER TABLE agent_memory_cards ADD COLUMN experience_notes TEXT DEFAULT '[]'")
except Exception:
    pass

# save_memory_card 中处理新字段
"""
pitfalls: json.dumps(card.get("pitfalls") or [])
experience_notes: json.dumps(card.get("experience_notes") or [])
"""
```

### 2.3 memory/cards.py — 踩坑提取

```python
def enrich_card_pitfalls(card: AgentMemoryCard, steps: list, llm: LLMPort) -> list[str]:
    """检测失败/重试模式，需要时调 LLM 生成踩坑文本"""
    if not steps or not llm:
        return []

    new_pitfalls = []
    intent = card.intent_summary

    for i in range(len(steps)):
        s = steps[i]
        if s.get("ok") is not False:
            continue

        # 分支: 失败后重试成功?
        if i + 1 < len(steps):
            nxt = steps[i + 1]
            if nxt.get("ok") and s.get("method") == nxt.get("method") and s.get("path") == nxt.get("path"):
                text = _llm_pitfall_retry(llm, intent, s, nxt)
                if text and text not in card.pitfalls:
                    new_pitfalls.append(text)
                continue

        # 分支: 普通失败
        text = _llm_pitfall_error(llm, intent, s)
        if text and text not in card.pitfalls:
            new_pitfalls.append(text)

    return new_pitfalls


def _llm_pitfall_error(llm: LLMPort, intent: str, step: dict) -> str | None:
    """调 LLM 生成失败的踩坑文本"""
    prompt = (
        f"你在执行「{intent}」流程时，以下步骤失败了:\n"
        f"步骤 {step.get('step')}: {step.get('method')} {step.get('path')}\n"
        f"返回: {str(step.get('result_preview', ''))[:200]}\n"
        f"错误: {step.get('error', '')}\n\n"
        f"请写出 1-2 句踩坑提醒（不超过 50 字，具体可操作）:"
    )
    try:
        resp = llm.chat([{"role": "user", "content": [{"type": "text", "text": prompt}]}])
        return resp.content.strip() if resp and resp.content else None
    except Exception:
        return None


def _llm_pitfall_retry(llm: LLMPort, intent: str, failed: dict, success: dict) -> str | None:
    """调 LLM 生成重试踩坑文本"""
    prompt = (
        f"你在执行「{intent}」流程时，以下步骤首次失败后重试成功:\n"
        f"步骤 {failed.get('step')}: {failed.get('method')} {failed.get('path')}\n"
        f"首次失败: {str(failed.get('result_preview', ''))[:200]}\n"
        f"重试成功: {str(success.get('result_preview', ''))[:100]}\n\n"
        f"请写出 1-2 句踩坑提醒（不超过 50 字，说明什么情况下会失败及如何避免）:"
    )
    try:
        resp = llm.chat([{"role": "user", "content": [{"type": "text", "text": prompt}]}])
        return resp.content.strip() if resp and resp.content else None
    except Exception:
        return None


def enrich_card_experience(card: AgentMemoryCard, llm: LLMPort) -> str | None:
    """累计执行 5 次时调 LLM 生成经验总结"""
    if card.success_count < 3 or card.success_count % 5 != 0:
        return None
    if not llm:
        return None

    steps_summary = " -> ".join(
        f"{s.get('method','')} {s.get('path','')}" for s in (card.steps or [])
    )[:300]

    prompt = (
        f"以下流程已成功执行 {card.success_count} 次:\n"
        f"意图: {card.intent_summary}\n"
        f"步骤: {steps_summary}\n"
        f"踩坑记录: {chr(10).join('- '+p for p in (card.pitfalls or []))}\n\n"
        f"请写出该流程的经验总结，包括:\n"
        f"- 标准操作顺序\n"
        f"- 需要特别注意的点\n"
        f"- 常见的变体或分支\n"
        f"不超过 100 字:"
    )
    try:
        resp = llm.chat([{"role": "user", "content": [{"type": "text", "text": prompt}]}])
        return resp.content.strip() if resp and resp.content else None
    except Exception:
        return None
```

### 2.4 agent.py — 在回合结束后调用 enrich

```python
# 在 chat_stream_events 中，record_successful_flow 之后

if all_steps_out:
    try:
        flow_hash = record_successful_flow(
            user_message, all_steps_out, step_counter[0],
            db=self.db, llm=self.llm, namespace=self.namespace,
        )
        update_graph_from_steps(all_steps_out, approved=False, db=self.db, namespace=self.namespace)

        # <-- 新增: 踩坑提取 + 经验总结
        if flow_hash and self.llm:
            card = _load_card_by_hash(self.db, flow_hash, self.namespace)
            if card:
                new_pitfalls = enrich_card_pitfalls(card, all_steps_out, self.llm)
                if new_pitfalls:
                    card.pitfalls.extend(new_pitfalls)
                exp = enrich_card_experience(card, self.llm)
                if exp and exp not in card.experience_notes:
                    card.experience_notes.append(exp)
                if new_pitfalls or exp:
                    self.db.save_memory_card(card.to_dict())

        # <-- 新增: memory.md 规则提取
        memory_entries = extract_memory_md_entries(user_message, reply, all_steps_out)
        if memory_entries:
            append_to_memory_md(_get_instance_dir(), memory_entries)

    except Exception as e:
        logger.error(f"[AgentMemory] error: {e}")
```

### 2.5 agent.py — memory.md 规则提取

```python
def extract_memory_md_entries(user_message: str, reply: str, steps: list) -> list[dict]:
    """纯字符串判断，不需要 LLM"""
    entries = []

    # H1: 用户自称
    for prefix in ["叫我", "我是", "叫我了"]:
        if prefix in user_message:
            idx = user_message.find(prefix) + len(prefix)
            name = user_message[idx:].split("。")[0].split("，")[0].split(" ")[0].strip()
            if name and len(name) <= 10:
                entries.append({"section": "user_info", "content": f"- Name: {name}"})
                break

    # H2: 用户偏好
    for kw in ["喜欢", "不要", "倾向", "偏好"]:
        if kw in user_message:
            idx = user_message.find(kw)
            text = user_message[idx:].split("。")[0].split("，")[0].strip()
            if 3 < len(text) < 60:
                entries.append({"section": "preference", "content": f"- {text}"})
                break

    # H3: 回复中的建议
    for kw in ["建议", "注意", "推荐", "以后"]:
        if kw in reply:
            idx = reply.find(kw)
            sentence = reply[idx:].split("。")[0].split("!")[0].strip()
            if 5 < len(sentence) < 100:
                entries.append({"section": "tips", "content": f"- {sentence}"})
                break

    # H4: 路径提取
    path_cmds = {"dir", "pwd", "where", "ls", "cd", "find"}
    for s in steps:
        if s.get("method") != "EXEC":
            continue
        cmd = (s.get("path") or "").strip().split()[0]
        if cmd not in path_cmds:
            continue
        for line in (s.get("result_preview") or "").split("\n"):
            line = line.strip()
            if re.match(r"^[A-Z]:\\", line):
                entries.append({"section": "project_context", "content": f"- Path: {line}"})
                break

    return entries


def append_to_memory_md(inst_dir: str, entries: list[dict]):
    """将条目写入 memory.md"""
    path = os.path.join(inst_dir, "memory.md")
    MAX_LINES = 200

    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("# NanoGhost Memory\n\n")

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    for entry in entries:
        section, line = entry["section"], entry["content"]
        if line in content:
            continue  # 去重
        header = f"## {section}"
        if header in content:
            content = content.replace(header, header + "\n" + line, 1)
        else:
            content += f"\n## {section}\n{line}\n"

    lines = content.split("\n")
    if len(lines) > MAX_LINES:
        content = "\n".join(lines[:MAX_LINES]) + "\n\n<!-- truncated -->"

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
```

### 2.6 tool/builtins.py — memory_write 工具

```python
MEMORY_WRITE_DEF = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["append", "update", "delete"],
            "description": "append: 追加 / update: 更新 / delete: 删除",
        },
        "section": {
            "type": "string",
            "description": "分类，如 user_info / preference / tips",
        },
        "content": {
            "type": "string",
            "description": "内容文本（append/update 时使用）",
        },
        "key": {
            "type": "string",
            "description": "定位关键字（update/delete 时用）",
        },
    },
    "required": ["action", "section"],
}


def memory_write(args: dict, ctx: dict) -> ToolResult:
    """写入/更新/删除 memory.md"""
    action = args["action"]
    section = args["section"]
    content = args.get("content", "")
    key = args.get("key", "")

    inst_dir = os.getenv("INSTANCE_DIR", "")
    if not inst_dir:
        return ToolResult(ok=False, error="INSTANCE_DIR not set")

    path = os.path.join(inst_dir, "memory.md")
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("# NanoGhost Memory\n\n")

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    if action == "append":
        header = f"## {section}"
        if header in text:
            text = text.replace(header, header + "\n" + content, 1)
        else:
            text += f"\n## {section}\n{content}\n"

    elif action == "update":
        old = f"- {key}:"
        if old in text:
            for line in text.split("\n"):
                if line.strip().startswith(old):
                    text = text.replace(line, f"- {key}: {content}")
                    break
        else:
            return ToolResult(ok=False, error=f"Not found: {old}")

    elif action == "delete":
        text = "\n".join(
            l for l in text.split("\n")
            if not l.strip().startswith(f"- {key}:")
        )

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    return ToolResult(ok=True, data=f"memory.md {action} ok")


# 在 register_builtins() 中注册
registry.register("memory_write", memory_write,
    description="记录需要记住的信息到 memory.md",
    parameters=MEMORY_WRITE_DEF, category="system")
```

### 2.7 engine/messages.py — 注入踩坑/经验

```python
# build_agent_messages_with_history() 中，历史流程注入改造

if similar_flows:
    lines = []
    for idx, m in enumerate(similar_flows, start=1):
        intent = (m.get("intent_summary") or "").strip()
        steps = m.get("steps") or []
        pitfalls = m.get("pitfalls") or []
        experiences = m.get("experience_notes") or []

        step_text = " -> ".join(
            f"{s.get('method','')} {s.get('path','')}" for s in steps[:5]
        )
        part = f"{idx}. {intent}\n   步骤: {step_text}"
        if pitfalls:
            part += "\n   踩坑: " + "; ".join(pitfalls[:3])
        if experiences:
            part += "\n   经验: " + "; ".join(experiences[:2])
        lines.append(part)

    mem_text = "【历史相似流程】\n" + "\n".join(lines) + "\n\n可参考这些流程。注意踩坑提醒。"
    out.append({"role": "system", "content": [{"type": "text", "text": mem_text}]})
```

### 2.8 run.py — memory.md 注入 system prompt

```python
def assemble_sys_prompt() -> str:
    parts = []
    # ... 原有 profile + rules 加载 ...

    # 注入 memory.md
    inst_dir = os.getenv("INSTANCE_DIR", "")
    if inst_dir:
        memory_path = os.path.join(inst_dir, "memory.md")
        if os.path.isfile(memory_path):
            try:
                memory_content = open(memory_path, encoding="utf-8").read().strip()
                if memory_content:
                    parts.append(f"## 记住的信息\n\n{memory_content}\n\n"
                                 f"如需更新，使用 memory_write 工具。")
            except Exception:
                pass

    return "\n\n".join(parts)
```

---

<a name="提示词"></a>
## 三、提示词更新（已完成）

### 3.1 prompts/agent_profile.md

```markdown
你是一个智能助手，通过调用可用工具来完成用户请求。

## 工作方式

- 每次回复可以使用一个或多个工具来完成任务
- 根据工具返回的结果决定下一步操作
- 任务完成后直接回复总结（无需输出 JSON）
- 如果信息不足，用 ask_user 工具询问用户
- 不要假设工具不可用——先用工具试试，出错了再告诉我

## 可用工具

### 系统工具
- `terminal` -> 执行 shell 命令、运行脚本、访问文件系统
- `read` -> 读取本地文件（绝对路径）
- `ask_user` -> 向用户提问等待回答

### 记事本工具
- `memory_write` -> 记录需要记住的信息
  - 用户说了个人信息时记下来
  - 发现了有用经验时记下来
  - 知道了项目配置时记下来
  - 格式: memory_write(action="append", section="分类", content="- 内容")

### 技能工具
- `skills_list` -> ...
- `use_skill(name)` -> ...

### 子代理工具
- `delegate_task` -> ...

## 记住信息

遇到以下情况时，用 memory_write 记下来：
- 用户告诉你称呼、身份、偏好
- 你发现了项目路径、端口、配置
- 你总结出了操作经验或发现了踩坑
- 用户明确说「记住这个」

不要记密码、token 等敏感信息。

## 行为规则

1. 使用工具完成任务，不要编造数据
2. 每次工具调用后，根据返回结果决定下一步
3. 若工具返回错误，尝试修复或询问用户
4. 不要重复执行相同的操作
5. 完成后直接回复用户总结即可
```

### 3.2 prompts/agent_rules_conduct.md

```markdown
## 行为规则

1. 使用工具完成任务，不要编造数据
2. 每次工具调用后，根据返回结果决定下一步
3. 若工具返回错误，尝试修复或询问用户
4. 不要重复执行相同的操作
5. 完成后直接回复用户总结即可

## 记忆规则

6. 上下文中出现「历史相似流程」时，参考其中的踩坑提醒和经验总结
   - 如果流程一致，优先按经验总结的标准方式操作
   - 如果步骤有踩坑提醒，执行该步骤时额外留意
7. 上下文中出现「记住的信息」时，这些是用户告诉你的事实和偏好
   - 操作时尊重用户的偏好（如喜欢简洁回复）
   - 涉及项目配置时参考记住的信息
8. 当用户告诉你个人信息、偏好时，用 memory_write 记下来
9. 当遇到一个错误并成功解决后，考虑是否要记到踩坑记录
10. 不要记敏感信息（密码、token）
```

---

<a name="配置"></a>
## 四、配置文件更新（已完成）

### 4.1 ~/.nanoghost/config.yaml — 全局 MCP 注册

```yaml
mcp_servers:
  capture:
    transport: stdio
    command: E:\OperationsAssistantORIG\Tech\Code\Capture\venv\Scripts\python.exe
    args:
      - E:\OperationsAssistantORIG\Tech\Code\Capture\server\mcp\capture_mcp_server.py
    timeout_seconds: 30
```

### 4.2 ~/.nanoghost/instances/cc/config.yaml — 实例配置

```yaml
skills:
  enabled_only:
    - lark-approval
    - lark-apps
    # ... 全部 26 个 lark 技能 ...

mcp:
  enabled_only:
    - capture
```

### 4.3 ~/.nanoghost/instances/cc/.env

```ini
AGENTS_SKILLS_DIR=~/.agents/skills
```

---

<a name="实施"></a>
## 五、实施顺序

| 优先级 | 步骤 | 文件 | 说明 |
|---|---|---|---|
| P0 | 1 | memory/models.py | Card 加 pitfalls, experience_notes 字段 |
| P0 | 2 | adapters/database.py | DB migration 加两列 |
| P0 | 3 | memory/cards.py | enrich_card_pitfalls() + enrich_card_experience() |
| P0 | 4 | agent.py | 在回合结束后调用 enrich 函数 |
| P0 | 5 | agent.py | extract_memory_md_entries() + append_to_memory_md() |
| P0 | 6 | tool/builtins.py | memory_write 工具 |
| P0 | 7 | engine/messages.py | 注入踩坑/经验到 prompt |
| P0 | 8 | run.py | memory.md 注入 system prompt |
| P0 | — | prompts/*.md | 已完成 |
| P1 | 9 | (可选) | memory.md 维护：超过 150 行时 LLM 精简 |
