# agent-core

从 Capture 项目中提取的独立 LLM Agent 框架。

## 特性

- **对象化** — `Agent` 类，多实例，每实例独立端口注入
- **多实例隔离** — 实例目录隔离配置/人格/记忆/工作目录；`namespace` 作为逻辑隔离标识
- **SKILL.md 生态** — 自动发现 opencode / claude-code / hermes 标准 SKILL.md 技能，市场即插即用
- **Skill 插拔** — `Skill` ABC，可注册自定义 action 处理器（向后兼容）
- **SubAgent** — 从主 Agent 派生子 Agent，继承端口，独立命名空间
- **飞书通道** — 通过 lark-oapi SDK 建立 WebSocket 长连接，接收消息并自动回复
- **Port/Adapter** — 5 个抽象端口（DatabasePort, LLMPort, HttpPort, ImagePort, ChannelPort）
- **MCP 集成（HTTP/SSE）** — 全局注册表 + 实例白名单，自动发现 tools 并注入 tool_calls
- **Hermes 风格 Gateway** — gateway 常驻进程托管飞书 worker，一键启停

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

## 多实例（多进程多机器人）

每个机器人一个独立实例目录（代码一份、数据多份），目录内可包含：

- `.env`：该实例的 LLM/飞书凭证等配置
- `prompts/`：该实例的人格与规则
- `data/agent_data.db`：该实例的记忆/会话数据库（自动创建）
- `config.yaml`：实例配置（包含 MCP/Skill 白名单）
- `work/`：shell 工作目录（自动创建）

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
├── gateway_server.py            # gateway 常驻服务（health/config/托管 worker）
├── prompts/
│   ├── agent_profile.md         # Agent 人设
│   └── agent_rules_conduct.md   # 行为规则
├── src/agent_core/
│   ├── agent.py                 # Agent 类（主入口）
│   ├── interfaces/              # 端口抽象
│   ├── engine/                  # 编排层
│   ├── memory/                  # 记忆系统
│   ├── skill/                   # Skill 扩展
│   ├── mcp/                     # MCP（HTTP/SSE）集成
│   ├── channel/feishu/          # 飞书通道
│   └── utils/                   # 工具函数
└── tests/
    └── test_basic.py            # 基础测试
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

## License

MIT
