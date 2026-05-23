# agent-core 使用指南

## 安装

```bash
pip install agent-core        # 核心
pip install agent-core[feishu] # 含飞书通道
```

## 快速开始

```python
from agent_core import Agent, AgentConfig
from agent_core.interfaces import DatabasePort, LLMPort, HttpPort

# 1. 实现端口（或者用 Capture 提供的适配器）
class MyDB(DatabasePort): ...
class MyLLM(LLMPort): ...
class MyHttp(HttpPort): ...

# 2. 创建 Agent 实例
agent = Agent(db=MyDB(), llm=MyLLM(), http=MyHttp(), namespace="my_app")

# 3. 对话
config = AgentConfig(
    base_url="http://localhost:8000",
    sys_prompt="你是助手,可以调用系统 API。",
    api_spec={"schemas": [...], "details": {...}},
)
for ev_type, ev_data in agent.chat_stream_events(
    "帮我创建一个监控任务",
    session_id="session-xxx",
    config=config,
):
    print(ev_type, ev_data)
```

## 多实例

```python
agent_a = Agent(db=db, llm=llm, http=http, namespace="app_a")
agent_b = Agent(db=db, llm=llm, http=http, namespace="app_b")
# 两个 Agent 的记忆完全隔离
```

## Skill 扩展

Agent-core 支持 SKILL.md 生态（兼容 opencode/claude-code/hermes）：

将 SKILL.md 文件放入生态标准目录即可自动发现：

```
项目目录/
├── .opencode/skills/<name>/SKILL.md    # 项目级
├── .claude/skills/<name>/SKILL.md
├── .agents/skills/<name>/SKILL.md
```

或全局目录：
- `~/.config/opencode/skills/<name>/SKILL.md`
- `~/.claude/skills/<name>/SKILL.md`
- `~/.agents/skills/<name>/SKILL.md`

SKILL.md 格式（与 opencode/claude-code 完全兼容）：

```markdown
---
name: git-release
description: Create consistent releases and changelogs
license: MIT
compatibility: opencode
metadata:
  audience: maintainers
---
## What I do
- Draft release notes from merged PRs
- Propose a version bump
```

Agent 初始化时自动发现所有可用技能，在 system prompt 中注入轻量索引（仅名称+描述）。

模型按需通过 `{"use_skill": "skill-name"}` 加载完整指令（与 Hermes/opencode 的 skill_view 模式兼容）：

```python
agent = Agent(db=db, llm=llm, http=http, auto_discover_skills=True)
# auto_discover_skills=True 为默认值

skill_defs = agent.list_skill_defs()
for s in skill_defs:
    print(f"{s.name}: {s.description}")

# 手动触发重新发现
agent.discover_skills(extra_dirs=["path/to/custom/skills"])

# 按意图匹配相关技能
matched = agent.match_skills("帮我发布新版本", top_k=3)
```

LLM 对话流中，Agent 识别到 `{"use_skill": "技能名称"}` 时自动加载对应 SKILL.md 完整内容注入上下文，
yield `("skill_loaded", {"name": "skill-name"})` 事件。
如果技能不存在则返回提示并列出可用技能。



## SubAgent

```python
child = agent.create_sub_agent("parallel-task", namespace="sub:task-1")
for ev_type, ev_data in child.chat_stream_events(
    "单独处理这个子任务", session_id="sub-session", config=config,
):
    print(ev_type, ev_data)
```

## 飞书通道

```python
import asyncio
from agent_core.channel.feishu import FeishuWSClient

client = FeishuWSClient(agent=agent, sys_prompt="...", api_spec={})
asyncio.run(client.run_forever())
```
