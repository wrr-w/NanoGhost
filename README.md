# agent-core

从 Capture 项目中提取的独立 LLM Agent 框架。

## 特性

- **对象化** — `Agent` 类，多实例，每实例独立端口注入
- **多实例隔离** — `namespace` 参数隔离记忆卡片，互不干扰
- **SKILL.md 生态** — 自动发现 opencode / claude-code / hermes 标准 SKILL.md 技能，市场即插即用
- **Skill 插拔** — `Skill` ABC，可注册自定义 action 处理器（向后兼容）
- **SubAgent** — 从主 Agent 派生子 Agent，继承端口，独立命名空间
- **飞书通道** — 通过 lark-oapi SDK 建立 WebSocket 长连接，接收消息并自动回复
- **Port/Adapter** — 5 个抽象端口（DatabasePort, LLMPort, HttpPort, ImagePort, ChannelPort）

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

## 目录结构

```
├── run.py                       # 启动入口
├── prompts/
│   ├── agent_profile.md         # Agent 人设
│   └── agent_rules_conduct.md   # 行为规则
├── src/agent_core/
│   ├── agent.py                 # Agent 类（主入口）
│   ├── interfaces/              # 端口抽象
│   ├── engine/                  # 编排层
│   ├── memory/                  # 记忆系统
│   ├── skill/                   # Skill 扩展
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
