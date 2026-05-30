# Memory System - Executable SPEC

> 每条规则都有具体的判断条件，可直接翻译为代码

---

## 1. Flow Card 踩坑提取

### 触发点

在 agent.py, record_successful_flow() 返回后调用:

```python
if all_steps_out:
    flow_hash = record_successful_flow(...)
    update_graph_from_steps(...)

    # NEW
    if flow_hash:
        enrich_card_with_experience(
            db=self.db,
            flow_hash=flow_hash,
            steps=all_steps_out,
            reply=reply,
        )
```

### R1: 失败后重试

```
IF steps[i].ok == False
   AND steps[i+1].ok == True
   AND steps[i].method == steps[i+1].method
   AND steps[i].path == steps[i+1].path
THEN
   card.pitfalls.append(
     "steps{i+1} ({method} {path}) first failed, retry success - may be unstable"
   )
```

### R2: Error 关键词匹配

```
IF steps[i].ok == False
   AND "error" / "timeout" / "auth" / "rate" in steps[i].result_preview
THEN
   MATCH keyword:
     "login|auth|token"  -> "may need re-authentication"
     "timeout"           -> "increase timeout or batch process"
     "rate|limit|429"    -> "rate limited, add delay between calls"
     "not found|404"     -> "resource may not exist, query first"
   card.pitfalls.append("step{step_num}: {suggestion}")
```

### R3: 多次执行总结

```
IF card.success_count >= 3
   AND card.success_count % 5 == 0
THEN
   LLM summarize(card.steps) -> card.experience_notes.append(result)
```

### R4: 去重

```
IF new_pitfall already in card.pitfalls (exact string match)
THEN skip
ELSE card.pitfalls.append(new_pitfall)
```

---

## 2. memory.md Hook 提取

### 触发点

```python
if all_steps_out:
    flow_hash = record_successful_flow(...)
    update_graph_from_steps(...)
    if flow_hash:
        enrich_card_with_experience(...)

    # NEW
    entries = extract_memory_from_conversation(
        user_message, reply, all_steps_out
    )
    if entries:
        append_to_memory_md(inst_dir, entries)
```

### H1: 用户偏好提取

```python
PREFERENCES = [
    (r"叫我[.:]?(.+)", "user_info", "Name: {0}"),
    (r"我是(.+)[.,]?", "user_info", "Name: {0}"),
    (r"喜欢(.+?)(?:的回复|的风格)", "preference", "Likes: {0}"),
    (r"不要(.+?)[.!]?", "preference", "Avoid: {0}"),
    (r"用(.+?)代替(.+?)", "preference", "Prefer {0} over {1}"),
    (r"我(?:的习惯|偏好)是(.+)", "preference", "Habit: {0}"),
]

def extract_preference(text):
    for pat, sec, tmpl in PREFERENCES:
        m = re.search(pat, text)
        if m:
            return {"section": sec, "content": "- " + tmpl.format(*m.groups())}
    return None
```

### H2: 错误重试 -> 踩坑

```python
def extract_retry_pitfall(steps, reply):
    for i in range(len(steps)-1):
        a, b = steps[i], steps[i+1]
        if (not a.get("ok")) and b.get("ok")            and a.get("method") == b.get("method")            and a.get("path") == b.get("path"):
            cause = extract_first_error_sentence(reply)
            return {
                "section": "pitfalls",
                "content": f"- {a['method']} {a['path']}: {cause}"
            }
    return None
```

### H3: 路径提取

```python
PATH_CMDS = ["dir", "pwd", "where", "ls", "cd", "find"]
def extract_path_info(steps):
    for s in steps:
        if s.get("method") != "EXEC":
            continue
        cmd = s.get("path", "")
        if not any(c in cmd for c in PATH_CMDS):
            continue
        for line in (s.get("result_preview") or "").split("\n"):
            if re.search(r"[A-Z]:\\(?:[^\\]+\\){2,}", line):
                return {
                    "section": "project_context",
                    "content": f"- Directory: {line.strip()}"
                }
    return None
```

### H4: 回复建议提取

```python
ADVICE_PATTERNS = [
    (r"(?:建议|推荐)(.+?)(?:[.,]|$)", "experience"),
    (r"注意(.+?)(?:[.,]|$)", "pitfalls"),
    (r"标准流程(.+?)(?:[.,]|$)", "experience"),
]
def extract_advice(reply):
    for pat, sec in ADVICE_PATTERNS:
        m = re.search(pat, reply)
        if m:
            return {"section": sec, "content": "- " + m.group(1).strip()}
    return None
```

---

## 3. memory.md 文件操作

```python
MEMORY_PATH = os.path.join(INSTANCE_DIR, "memory.md")
MAX_LINES = 200

def append_to_memory_md(entries):
    if not os.path.isfile(MEMORY_PATH):
        with open(MEMORY_PATH, "w") as f:
            f.write("# NanoGhost Memory\n\n")

    with open(MEMORY_PATH, "r") as f:
        content = f.read()

    for entry in entries:
        section = entry["section"]
        line = entry["content"]
        if line in content:
            continue  # dedup

        header = f"## {section}"
        if header in content:
            content = content.replace(header, header + "\n" + line, 1)
        else:
            content += f"\n## {section}\n{line}\n"

    # Truncate
    lines = content.split("\n")
    if len(lines) > MAX_LINES:
        content = "\n".join(lines[:MAX_LINES])
        content += "\n\n<!-- truncated -->"

    with open(MEMORY_PATH, "w") as f:
        f.write(content)


def memory_write(args, ctx) -> ToolResult:
    action = args["action"]   # append | update | delete
    section = args["section"]
    content = args.get("content", "")
    key = args.get("key", "")

    path = MEMORY_PATH
    if not os.path.isfile(path):
        with open(path, "w") as f:
            f.write("# NanoGhost Memory\n\n")

    with open(path, "r") as f:
        text = f.read()

    if action == "append":
        header = f"## {section}"
        if header in text:
            text = text.replace(header, header + "\n" + content, 1)
        else:
            text += f"\n## {section}\n{content}\n"

    elif action == "update":
        old = f"- {key}:"
        new = f"- {key}: {content}"
        if old in text:
            text = text.replace(old, new, 1)
        else:
            return ToolResult(ok=False, error=f"Not found: {old}")

    elif action == "delete":
        lines = [l for l in text.split("\n")
                 if not l.strip().startswith(f"- {key}:")]
        text = "\n".join(lines)

    with open(path, "w") as f:
        f.write(text)
    return ToolResult(ok=True, data=f"memory.md {action} ok")
```

---

## 4. 读取注入改造

### 4.1 memory.md -> system prompt

在 assemble_sys_prompt() 末尾:

```python
memory_path = os.path.join(inst_dir, "memory.md")
if os.path.isfile(memory_path):
    content = open(memory_path).read().strip()
    if content:
        parts.append(
            "## Remembered\n\n"
            f"{content}\n\n"
            "Use memory_write tool to update."
        )
```

### 4.2 Flow Card -> system message

在 messages.py build_agent_messages_with_history():

```python
if similar_flows:
    lines = []
    for idx, m in enumerate(similar_flows, start=1):
        intent = (m.get("intent_summary") or "").strip()
        steps = m.get("steps") or []
        pitfalls = m.get("pitfalls") or []
        experiences = m.get("experience_notes") or []

        step_text = " -> ".join(
            f"{s.get('method','')} {s.get('path','')}"
            for s in steps[:5]
        )
        part = f"{idx}. {intent}\n   Steps: {step_text}"
        if pitfalls:
            part += "\n   Warnings: " + "; ".join(pitfalls[:3])
        if experiences:
            part += "\n   Tips: " + "; ".join(experiences[:2])
        lines.append(part)

    mem_text = (
        "[Similar Past Flows]\n"
        + "\n".join(lines)
        + "\n\nRefer to these. Pay attention to warnings."
    )
    out.append({"role": "system", "content": [{"type": "text", "text": mem_text}]})
```

---

## 5. Prompt 改造 (agent_profile.md)

```
### Memory Tools
- `memory_write` -> Write/update memory.md
  - User says personal info -> save to user_info section
  - Encountered then fixed an error -> save to pitfalls section
  - Discovered a pattern -> save to experience section

## How to work
- Collect user preferences and save with memory_write
- After fixing errors, record the fix as a pitfall
- Memory content is injected at the start of each conversation
```

---

## Implementation Order

| Step | File | Change |
|------|------|--------|
| 1 | memory/models.py | Add pitfalls, experience_notes, branch_hints to AgentMemoryCard |
| 2 | adapters/database.py | DB migration: add 3 columns to agent_memory_cards |
| 3 | memory/cards.py | Add enrich_card_with_experience() with R1-R4 |
| 4 | agent.py | Call enrich_card after record_successful_flow |
| 5 | agent.py | Add extract_memory_from_conversation() + append_to_memory_md() |
| 6 | tool/builtins.py | Add memory_write tool |
| 7 | engine/messages.py | Update injection template for pitfalls/experiences |
| 8 | run.py | Inject memory.md into system prompt |
| 9 | prompts/agent_profile.md | Add memory tool description |
