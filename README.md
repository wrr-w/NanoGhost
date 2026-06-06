# NanoGhost

> 从 Capture 项目中提取的独立 LLM Agent 框架。
> 对象化、多实例、可插拔，内置分层记忆系统。

## 架构概览

```
┌─────────────────────────────────────────────┐
│  Agent (对象化，多实例隔离)                   │
│  ├── 端口注入: DB / LLM / HTTP / Image       │
│  ├── ToolRegistry (Hermes function calling)  │
│  ├── SkillRegistry (SKILL.md 生态)           │
│  ├── HookBus (生命周期钩子)                  │
│  └── Memory System v3 (三层存储)             │
│       ├── Card   → 完整流程记录              │
│       ├── Graph  → 步骤转移统计 (L1~L4)      │
│       └── memory.md → 日志与决策             │
└─────────────────────────────────────────────┘
```

## 核心特性

- **对象化** — `Agent` 类，多实例，每实例独立端口注入与命名空间隔离
- **分层记忆系统 v3** — Card + Graph + memory.md 三层存储，统一分层披露查询模式
- **SKILL.md 生态** — 自动发现 opencode / claude-code / hermes 标准 SKILL.md 技能，市场即插即用
- **Skill 插拔** — `Skill` ABC，可注册自定义 action 处理器（向后兼容）
- **SubAgent** — 从主 Agent 派生子 Agent，继承端口，独立命名空间
- **飞书通道** — 通过 lark-oapi SDK 建立 WebSocket 长连接，接收消息并自动回复
- **Port/Adapter** — 5 个抽象端口（DatabasePort, LLMPort, HttpPort, ImagePort, ChannelPort）
- **MCP 集成** — 支持 HTTP/SSE 与 stdio 传输，全局注册表 + 实例白名单，自动发现 tools
- **Hermes 风格 Gateway** — gateway 常驻进程托管飞书 worker，一键启停
- **DeepSeek 支持** — 完整支持 `reasoning_content` 捕获与透传

## 快速开始

```bash
# 安装
pip install -r requirements.txt

# 配置
copy .env.example .env
# 编辑 .env，填入 FEISHU_APP_ID, FEISHU_APP_SECRET 和 LLM 配置

# 运行（飞书 WebSocket 模式）
python run.py
```

## 记忆系统 v3（Memory System）

NanoGhost 的核心差异化能力。三套存储模块遵循统一的分层披露查询模式：

```
Layer 1 (Index)   → 展示可用分类/概览
Layer 2 (Locate)  → 聚焦某一区域，获取摘要级内容
Layer 3 (Detail)  → 展开特定记录，获取完整内容
```

| 模块 | 存储内容 | 写入方式 | 查询接口 |
|------|---------|---------|---------|
| **Card** | 完整流程记录（意图 + 步骤 + 经验） | 回合结束后自动写入 `record_successful_flow()` + LLM 总结 | `list_card_index()` → `retrieve_similar_flows()` → `get_card_detail()` |
| **Graph** | 步骤转移统计（L1~L4 多层边） | 从 Card.steps 自动切割 | `memory_explore('node')` → `memory_explore('drill')` |
| **memory.md** | daily_log + decisions | Agent 通过 `memory_write` 工具自主写入 | `memory_read('index')` → `memory_read('section')` → `memory_read('detail')` |

### 回合后触发序列

```
Turn ends (有 tool calls + 有 LLM 回复)
  │
  ├─ [Phase 1] 写入 Card (record_successful_flow)
  ├─ [Phase 2] 写入 Graph (update_graph_from_steps)
  ├─ [Phase 3] LLM 总结 experience (单次调用，非每步)
  └─ [Phase 4] Agent 通过 memory_write 工具写入 memory.md
```

### OpCode 四层编码

Graph 使用四级编码对操作进行分类，支持从粗到细的多层查询：

- **L1** — Domain（协议/服务维度）
- **L2** — Action（工具名称维度）
- **L3** — Resource（工具 + 路径模式）
- **L4** — Detail（工具 + 完整路径）

### 内置记忆工具

- `memory_read` — 读取记忆（index / section / detail）
- `memory_write` — 写入记忆（append / update / delete）
- `memory_explore` — 探索操作图（node / drill）

详细设计见 [`docs/memory-system-v3-spec.md`](docs/memory-system-v3-spec.md)。

## 多实例（多进程多机器人）

每个机器人一个独立实例目录（代码一份、数据多份），目录内可包含：

- `.env`：该实例的 LLM/飞书凭证等配置
- `prompts/`：该实例的人格与规则
- `data/agent_data.db`：该实例的记忆/会话数据库（自动创建）
- `config.yaml`：实例配置（包含 MCP/Skill 白名单）
- `work/`：shell 工作目录（自动创建）
- `memory.md`：该实例的日志与决策记录（自动创建）

启动时指定实例目录：

```bash
python run.py -I <INSTANCE_DIR>
```

## Gateway（一键启动，托管飞书）

启动 gateway（后台）：

```bash
nanoghost gateway start -I <INSTANCE_DIR>
```

查看状态：

```bash
nanoghost gateway status -I <INSTANCE_DIR>
```

停止：

```bash
nanoghost gateway stop -I <INSTANCE_DIR>
```

说明：

- `--port` 可选，不传会自动分配空闲端口（端口会写入 `<INSTANCE_DIR>/runtime/gateway.json`）
- `-I` 既可以传绝对路径，也可以直接传实例名（如 `-I capture`），默认实例根目录为 `~/.nanoghost/instances/`
  - 如需自定义实例根目录：设置 `NANOGHOST_INSTANCES_ROOT`
- 查看所有实例：

```bash
nanoghost instance list
```

## MCP（无 UI，tool_calls 方式）

全局配置（一次配置，多实例复用）：

- `~/.nanoghost/config.yaml`

实例白名单：

- `<INSTANCE_DIR>/config.yaml`：`mcp.enabled_only: [server1, server2]`

运维命令：

```bash
nanoghost mcp list
nanoghost mcp probe -I <INSTANCE_DIR>
nanoghost mcp tools <server_id> -I <INSTANCE_DIR>
nanoghost mcp reload -I <INSTANCE_DIR>
```

## Skill（全局全集 + 实例白名单）

全局技能目录可通过环境变量 `AGENTS_SKILLS_DIR` 指定，默认 `~/.agents/skills`。

实例白名单在 `<INSTANCE_DIR>/config.yaml` 中声明：

```yaml
skills:
  enabled_only:
    - lark-calendar
    - lark-im
```

缺失/空列表时，该实例默认不加载任何全局技能（最安全）。

## 目录结构

```
├── run.py                       # 启动入口
├── gateway_server.py            # Gateway 常驻服务（health/config/托管 worker）
├── prompts/
│   ├── agent_profile.md         # Agent 人设
│   └── agent_rules_conduct.md   # 行为规则
├── docs/
│   ├── memory-system-v3-spec.md # 记忆系统 v3 设计文档（当前唯一 truth source）
│   ├── complete-changelist.md   # 完整改造清单
│   └── usage.md                 # 使用指南
├── src/agent_core/
│   ├── agent.py                 # Agent 主类
│   ├── cli.py                   # CLI 命令行接口
│   ├── interfaces/              # 端口抽象（DB/LLM/HTTP/Image/Channel）
│   ├── adapters/                # 适配器实现（LLM/DB/HTTP/Image）
│   ├── engine/                  # 编排层（config/messages/executor/feedback/hooks）
│   ├── memory/                  # 记忆系统 v3
│   │   ├── cards.py             # Card 存储（流程记录）
│   │   ├── graph.py             # Graph 存储（步骤转移）
│   │   ├── classifier.py        # OpCode 四层编码分类器
│   │   └── embedding.py         # 向量化
│   ├── skill/                   # Skill 扩展（发现/注册/模型）
│   ├── tool/                    # 工具层（注册表/内建工具/模型/搜索）
│   ├── mcp/                     # MCP 集成（HTTP/SSE/stdio/配置）
│   ├── channel/feishu/          # 飞书通道（WS/API）
│   └── utils/                   # 工具函数
└── tests/
    ├── test_basic.py            # 基础测试
    └── test_skill_discovery.py  # Skill 发现测试
```

## 编程使用

```python
from agent_core import Agent, AgentConfig
from agent_core.interfaces import DatabasePort, LLMPort, HttpPort

class MyDB(DatabasePort): ...
class MyLLM(LLMPort): ...
class MyHttp(HttpPort): ...

agent = Agent(db=MyDB(), llm=MyLLM(), http=MyHttp(), namespace="my_app")

config = AgentConfig(
    base_url="http://localhost:8000",
    sys_prompt="你是助手",
    api_spec={},
)

for ev_type, ev_data in agent.chat_stream_events(
    "帮我创建一个任务",
    session_id="session-xxx",
    config=config,
):
    print(ev_type, ev_data)
```

## 最近更新

### 2026-06-06 — Memory System v3 核心改造完成

- **重构记忆系统**：删除 7 份旧版文档，统一为 `docs/memory-system-v3-spec.md`
- **新增三层存储**：Card（流程记录）+ Graph（多层转移统计）+ memory.md（日志决策）
- **新增 classifier.py**：OpCode 四层编码分类器（L1~L4）
- **简化设计**：移除 pitfalls、scoring、pruning、approved_count 等冗余概念
- **统一查询模式**：Index → Locate → Detail 分层披露
- **内置记忆工具**：`memory_read`、`memory_write`、`memory_explore`
- **LLM 调用优化**：experience 总结每回合只调一次 LLM
- **文件统计**：21 个文件变更，+2520 行 / -2342 行

### 此前已完成

- DeepSeek `reasoning_content` 完整支持（5 个文件联动改造）
- 多实例隔离 + Gateway 常驻进程 + MCP HTTP/SSE + stdio 双传输
- SKILL.md 生态自动发现与按需加载

## License

MIT
