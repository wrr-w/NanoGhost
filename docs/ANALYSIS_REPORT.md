# NanoGhost vs learn-claude-code 对比分析报告

> 生成日期：2026-06-08

---

## 目录

1. [项目概述](#1-项目概述)
2. [架构对比](#2-架构对比)
3. [核心循环对比](#3-核心循环对比)
4. [模块逐层对比](#4-模块逐层对比)
5. [NanoGhost 优势](#5-nanoghost-优势)
6. [NanoGhost 劣势与缺失](#6-nanoghost-劣势与缺失)
7. [具体优化建议](#7-具体优化建议)
8. [SWOT 分析](#8-swot-分析)
9. [综合评分矩阵](#9-综合评分矩阵)

---

## 1. 项目概述

### NanoGhost

独立 LLM Agent 框架，从更大的 "Capture" 项目中提取而来。核心定位：

- **面向生产部署**的 bot 框架：多实例、多通道、Gateway 进程管理
- **飞书深度集成**：WebSocket 实时消息、完整 API 覆盖
- **三层记忆系统**：Cards（流程记忆）+ Graph（操作图谱）+ memory.md（用户档案）
- **SKILL.md 生态兼容**：支持 opencode/claude-code/hermes 标准技能
- **MCP 协议**：HTTP/SSE + stdio 双传输
- **PyInstaller 打包**：一键部署为 Windows .exe

### learn-claude-code

教育型 Agent 框架，shareAI-lab 出品。核心定位：

- **从 0 到 1 教学**：20 课逐步构建 agent harness
- **解构 Claude Code 核心模式**：单循环 + 工具调度 + 渐进式能力叠加
- **Harness 哲学**：模型提供智能，harness 提供工具/知识/边界
- **完整功能栈**：Permission、Hooks、TodoWrite、SubAgent、Skill、Context Compaction、Memory、Error Recovery、Task System、Background Tasks、Cron、Agent Teams、Worktree Isolation、MCP

---

## 2. 架构对比

### NanoGhost 架构

```
Entry Points:  run.py / cli.py / gateway_server.py
                    ↓
Core:   Agent (DI) → AgentExecutor (120轮循环)
        ├── LLMPort (OpenAILLM)    ← interfaces/
        ├── DatabasePort (SQLite)  ← adapters/
        ├── ImagePort (SQLite)     ←
        ├── ToolRegistry           ← tool/
        ├── SkillRegistry          ← skill/
        ├── MCPManager             ← mcp/
        └── MemorySystem (3层)     ← memory/
                    ↓
Channel: ChannelPort → ChannelIO → FeishuIO
         BotInstance / SessionStore / ContextBuilder
         FeishuWSClient (WebSocket 长连接)
```

**特点**：面向对象 DI 模式，端口-适配器分离，通道抽象层，Gateway 多进程管理

### learn-claude-code 架构

```
Entry Point:  code.py (单文件可运行)
                    ↓
Core:   agent_loop() → while True → model.call() → dispatch tools
        ├── TOOL_HANDLERS (dispatch map)
        ├── PermissionRules
        ├── HookBus (PreToolUse / PostToolUse)
        ├── TodoWrite
        ├── SubAgent
        ├── SkillLoader
        ├── ContextCompactor
        ├── MemorySystem
        ├── TaskBoard (JSON 文件持久化)
        ├── BackgroundJobManager
        ├── CronScheduler
        ├── TeamManager + Mailbox
        ├── WorktreeManager
        └── MCPRouter
```

**特点**：函数式/过程式设计，极度简洁，每课一个关注点，循环不变式贯穿始终

### 关键架构差异

| 维度 | NanoGhost | learn-claude-code |
|------|-----------|-------------------|
| 设计风格 | OOP + DI + 端口适配器 | 过程式 + dispatch map |
| 循环实现 | AgentExecutor 类，异步生成器 | 简单 while True 同步函数 |
| 工具注册 | ToolRegistry 类 + check_fn | 字典 dispatch map |
| 通道抽象 | 完整 ChannelPort/ChannelIO 抽象 | 无（CLI only） |
| 多实例 | Gateway 多进程管理 | 无 |
| 部署 | PyInstaller .exe + 进程管理 | pip install + 单文件运行 |
| 代码行数 | ~8000+ 行 | ~每课 200-500 行，总计 ~8000 |

---

## 3. 核心循环对比

### learn-claude-code（最简形态）

```python
def agent_loop(messages):
    while True:
        response = model.call(messages, tools=TOOLS)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return response.text
        for block in response.content:
            if block.type == "tool_use":
                output = TOOL_HANDLERS[block.name](**block.input)
                messages.append({"role": "user", "content": [output]})
```

**设计哲学**：循环本身永不变，扩展靠注册新 handler。每次加新功能是"往循环周围加东西"，而非"改循环本身"。

### NanoGhost（当前形态）

```python
class AgentExecutor:
    async def run(self, ...):
        for round_idx in range(max_rounds):
            # 1. 构建消息（含历史、记忆、技能上下文）
            messages = self._build_messages(...)
            # 2. 调用 LLM
            response = await self.llm.stream_chat(messages)
            # 3. 解析工具调用
            tool_calls = self._parse_tool_calls(response)
            # 4. 分发执行
            for tc in tool_calls:
                result = self.tools.dispatch(tc)
                yield ToolResultEvent(...)
            # 5. 后处理（fire-and-forget）
            asyncio.create_task(self._postprocess(...))
```

**问题**：

- 循环与构建消息、后处理紧耦合
- `max_rounds` 硬编码
- 同步工具在 `asyncio.to_thread()` 中运行
- 工具调用不支持流式返回

---

## 4. 模块逐层对比

### 4.1 工具系统

| 特性 | NanoGhost | learn-claude-code | 评价 |
|------|-----------|-------------------|------|
| 工具注册 | ToolRegistry 类 + check_fn | 字典 dispatch map | l-c-c 更简洁 |
| 工具定义 | ToolDefinition dataclass → OpenAI schema | 函数 + 内联 schema | 各有优劣 |
| 内置工具数 | 10+（terminal, read, skills, memory, delegate, ask_user） | 10+（bash, read, write, edit, glob, grep, web_fetch...） | l-c-c 编码工具更完整 |
| **缺失工具** | **grep, glob, edit, write, web_fetch** | 全有 | ⚠️ 重大缺失 |
| 工具参数验证 | 无 | 无 | 两者都缺失 |
| 工具超时 | shell 有 120s 超时 | shell 有 120s 超时 | 相当 |

**NanoGhost 问题**：

- `builtins.py` 2189 行过于庞大，需要拆分为独立文件
- 行间双倍空白，增加文件体积
- 缺少 grep/glob/edit/write 等编码核心工具
- `delegate_task` 在同步 handler 中调用 `asyncio.run()`，可能冲突

### 4.2 权限系统

| 特性 | NanoGhost | learn-claude-code | 评价 |
|------|-----------|-------------------|------|
| 文件访问限制 | workspace 外禁止 | PermissionRule + 审批管道 | l-c-c 更完善 |
| 命令安全 | shell=True（无限制） | 危险模式黑名单 + 审批 | l-c-c 更安全 |
| 用户审批 | ask_user 工具 | 结构化审批协议 | l-c-c 更正式 |
| 信任边界 | 无 | 分层信任 | ⚠️ NanoGhost 缺失 |

### 4.3 上下文管理

| 特性 | NanoGhost | learn-claude-code | 评价 |
|------|-----------|-------------------|------|
| 消息截断 | 数量 + token 估算双阶段 | microCompact + autoCompact + 磁盘存档 | l-c-c 分层更细致 |
| token 估算 | 字符数/3（粗糙） | 字符数/3（同样粗糙） | 相当 |
| 上下文压缩 | 无自动压缩 | 50k token 阈值自动触发 | ⚠️ NanoGhost 缺失 |
| 历史存档 | 无 | JSONL transcript archive | ⚠️ NanoGhost 缺失 |
| 压缩分级 | 无 | 3 层（micro/snip/auto） | ⚠️ NanoGhost 缺失 |

### 4.4 任务系统

| 特性 | NanoGhost | learn-claude-code | 评价 |
|------|-----------|-------------------|------|
| 任务持久化 | 无（仅 memory cards） | TaskBoard JSON 文件 + 依赖图 | ⚠️ NanoGhost 缺失 |
| 依赖关系 | 无 | blockedBy 单向依赖 | ⚠️ NanoGhost 缺失 |
| 后台执行 | 无独立后台任务 | BackgroundJob + 通知队列 | ⚠️ NanoGhost 缺失 |
| 定时调度 | 无 | CronScheduler | ⚠️ NanoGhost 缺失 |

### 4.5 错误恢复

| 特性 | NanoGhost | learn-claude-code | 评价 |
|------|-----------|-------------------|------|
| LLM 重试 | 无 | 有（retry + fallback model） | ⚠️ NanoGhost 缺失 |
| 错误降级 | 静默吞错 | 结构化降级 | NanoGhost 差 |
| 工具重试 | 无 | token 升级 + 重试 | ⚠️ NanoGhost 缺失 |

### 4.6 记忆系统

| 特性 | NanoGhost | learn-claude-code | 评价 |
|------|-----------|-------------------|------|
| 向量记忆 | Cards + MMR 多样性 | selection + extraction + consolidation | NanoGhost 更深 |
| 图谱记忆 | OpCode 4 级分类图谱 | 无 | ✅ NanoGhost 独有 |
| 用户档案 | memory.md 自动生成 | 无 | ✅ NanoGhost 独有 |
| 经验学习 | LLM 总结 + 反馈追踪 | 无 | ✅ NanoGhost 独有 |
| 记忆扩展性 | O(n) 全量加载 | 磁盘持久化 | NanoGhost 有扩展风险 |

**NanoGhost 记忆系统是其最大亮点**，三层设计（Cards + Graph + memory.md）远超 learn-claude-code 的简化实现。

### 4.7 多智能体协作

| 特性 | NanoGhost | learn-claude-code | 评价 |
|------|-----------|-------------------|------|
| SubAgent | 有（general/explore 预设） | 有（角色型） | 相当 |
| Agent Teams | 无 | 有（命名队友 + mailbox） | ⚠️ NanoGhost 缺失 |
| 队友通信 | 无 | JSONL mailbox 协议 | ⚠️ NanoGhost 缺失 |
| 自主领取任务 | 无 | 有（idle scan + auto-claim） | ⚠️ NanoGhost 缺失 |
| Worktree 隔离 | 无 | 有（任务绑定目录） | ⚠️ NanoGhost 缺失 |

### 4.8 Hooks 系统

| 特性 | NanoGhost | learn-claude-code | 评价 |
|------|-----------|-------------------|------|
| 事件类型 | 6 个（tool 前后、presenter 等） | 多个（PreToolUse/PostToolUse/...） | l-c-c 更全面 |
| 管道转换 | 有（收集非 None 结果） | 有 | 相当 |
| 生命周期事件 | 无 on_turn_start/end | 有 | ⚠️ NanoGhost 缺失 |

### 4.9 技能系统

| 特性 | NanoGhost | learn-claude-code | 评价 |
|------|-----------|-------------------|------|
| SKILL.md 解析 | 完整 frontmatter 解析器 + Hermes 元数据 | SkillManifest + 按需加载 | NanoGhost 更深 |
| 自动发现 | 目录扫描 + mtime 缓存 | 静态目录 | NanoGhost 更好 |
| 按需加载 | 有（lazy load） | 有（tool 触发） | 相当 |
| 技能安装 | npx skills add + 自动重发现 | 无 | ✅ NanoGhost 独有 |
| 变量替换 | ${HERMES_SKILL_DIR} 等 | 无 | ✅ NanoGhost 独有 |

### 4.10 MCP 集成

| 特性 | NanoGhost | learn-claude-code | 评价 |
|------|-----------|-------------------|------|
| HTTP/SSE | 完整实现 | 有 | 相当 |
| stdio | 完整实现 | 有 | 相当 |
| 服务器管理 | MCPManager + cooldown + 探针 | 简单路由 | NanoGhost 更完善 |
| 工具哈希去重 | 有 | 无 | ✅ NanoGhost 更好 |
| Action 白名单 | instance 级 config.yaml | 无 | ✅ NanoGhost 更好 |
| 失败阈值 | 可配置 | 无 | ✅ NanoGhost 更好 |

### 4.11 通道/平台集成

| 特性 | NanoGhost | learn-claude-code | 评价 |
|------|-----------|-------------------|------|
| 通道抽象 | ChannelPort + ChannelIO | 无（CLI only） | ✅ NanoGhost 独有 |
| 飞书集成 | WebSocket + 完整 REST API | 无 | ✅ NanoGhost 独有 |
| 消息范式转换 | 事件→规范化消息 | 无 | ✅ NanoGhost 独有 |
| Session 管理 | SessionStore + TTL | 无 | ✅ NanoGhost 独有 |
| 多 bot 隔离 | 完全隔离的 instance | 无 | ✅ NanoGhost 独有 |

### 4.12 部署与运维

| 特性 | NanoGhost | learn-claude-code | 评价 |
|------|-----------|-------------------|------|
| PyInstaller .exe | 有 | 无 | ✅ NanoGhost 更好 |
| Gateway HTTP API | 健康检查 + 启动/停止/重启 | 无 | ✅ NanoGhost 独有 |
| 进程管理 | WorkerManager + PID 文件 | 无 | ✅ NanoGhost 独有 |
| 配置管理 | .env + config.yaml + channel_directory | .env | NanoGhost 更灵活 |
| 原子写操作 | temp + rename | 无 | ✅ NanoGhost 更好 |

---

## 5. NanoGhost 优势

### 5.1 生产级运维设计
- Gateway 多进程管理 + REST API 实现真正的多 bot 部署
- PyInstaller 打包为一键部署 .exe
- 实例级隔离：独立数据库、prompts、技能、命名空间

### 5.2 记忆系统创新
- **三层记忆**（Cards + Graph + memory.md）是真正的创新
- MMR 多样性重排序避免记忆冗余
- OpCode 4 级操作分类图谱支持跨流程模式识别
- 经验学习循环（LLM 总结 + 用户反馈追踪）

### 5.3 通道抽象
- ChannelPort/ChannelIO 抽象使新平台接入成本低
- 飞书深度集成（WS 长连接 + 完整 API + 回复/表情/文件）
- 消息范式转换（事件→MessageContext→规范化）是优秀设计

### 5.4 MCP 集成成熟度
- Cooldown 机制 + 失败阈值 + 探针 TTL 达到生产级
- 工具哈希去重避免重复注册
- Instance 级白名单控制

### 5.5 技能生态兼容
- 完整 SKILL.md frontmatter 解析器（含 Hermes 扩展元数据）
- npx skills add 在线安装
- 模板变量替换

### 5.6 端口-适配器架构
- DI 设计使测试和扩展更容易
- 接口/适配器分离清晰
- SubAgent 继承端口实现代码复用

---

## 6. NanoGhost 劣势与缺失

### 6.1 🔴 严重问题

| 问题 | 位置 | 影响 |
|------|------|------|
| **import 路径错误** | `engine/agent.py:82` — `agent_core.infra.config_loader` 不存在 | MCP 永不初始化 |
| **无代码编辑工具** | 缺失 grep/glob/edit/write | 无法作为编码 agent 使用 |
| **无上下文自动压缩** | 长对话会达到 token 上限崩溃 | 无法长时间工作 |
| **无 LLM 重试机制** | API 错误直接返回给用户 | 用户体验差 |
| **线程安全问题** | ContextBuilder 类级状态、memory.md 无锁写入 | 并发量高时数据损坏 |

### 6.2 🟡 中等问题

| 问题 | 说明 |
|------|------|
| **无任务持久化** | 无法将大型目标分解为持久化任务并追踪依赖 |
| **无后台命令执行** | 长时间命令阻塞整个 agent 循环 |
| **无 Agent Teams** | 无命名队友、无 mailbox 通信、无自主领取任务 |
| **无 cron 调度** | 无法定时触发任务 |
| **无 worktree 隔离** | 并行任务无目录级隔离 |
| **Token 估算粗糙** | 字符/3 对中英文误差大，缺少 tiktoken |
| **Memory 扩展风险** | `load_all_memory_cards()` O(n) 全量加载 |
| **builtins.py 2189 行** | 过度庞大的单体文件 |
| **同步核心** | LLM adapter 和 tool handler 全同步，吞吐受限 |

### 6.3 🟢 轻微问题

| 问题 | 说明 |
|------|------|
| 无 session resume/checkpoint | 重启丢失未完成工作 |
| 无 LSP 集成 | 无法利用语言服务器做智能重构 |
| 无 Git 感知 | 无法利用 git diff/status 做上下文理解 |
| Hooks 事件不足 | 缺少 on_turn_start/end、on_message_received |
| 无 config 验证 | 空 LLM_API_KEY 导致静默失败 |
| ToolResult 截断 4000 字符 | 大工具结果可能丢失关键信息 |
| certifi PyInstaller 兼容 | 已修复但未验证 |

---

## 7. 具体优化建议

### 7.1 立即修复（P0）

```yaml
1. 修复 import 路径错误:
   文件: engine/agent.py:82
   修改: from agent_core.config import load_instance_config

2. 增加核心编码工具:
   - grep:  正则搜索文件内容
   - glob:  文件名模式匹配
   - edit:  精确字符串替换编辑
   - write: 创建/覆写文件

3. 增加 LLM 重试:
   - 指数退避 (1s/2s/4s/8s)
   - 最多 3 次重试
   - 可配置 fallback model
```

### 7.2 短期优化（P1）

```yaml
4. 上下文自动压缩:
   参考 learn-claude-code 的 3 层策略:
   - microCompact: 旧工具结果压缩为占位符（保留最近 3 轮）
   - autoCompact:  超过 50k token 时触发 LLM 摘要
   - 压缩前保存完整 transcript 到 JSONL 存档

5. 任务系统:
   - TaskBoard: JSON 文件持久化 + 依赖图
   - BackgroundJob: 后台命令执行 + 通知注入
   - TaskRecord: status/pending/in_progress/completed

6. 拆分 builtins.py:
   builtins/terminal.py
   builtins/files.py (read/write/edit/grep/glob)
   builtins/skills.py
   builtins/memory.py
   builtins/delegate.py
   builtins/ask.py
```

### 7.3 中期增强（P2）

```yaml
7. Agent Teams:
   - Teammate: 命名持久队友 + role + system_prompt
   - Mailbox:   每个队友的 JSONL 消息队列
   - Protocol:  shutdown handshake + plan approval
   - AutoClaim: idle 队友自动从 task board 领取任务

8. Worktree 隔离:
   - git worktree 为每个任务创建独立工作目录
   - 任务-目录绑定（JSON 索引）
   - 生命周期管理（创建/保留/删除）

9. ContextBuilder 线程安全:
   - 实例级状态替代类级状态
   - memory.md 文件锁保护并发写入
```

### 7.4 长期愿景（P3）

```yaml
10. Cron 调度器:
    - 持久化调度规则
    - session-scoped 触发
    - agent 可自行创建/修改/删除定时任务

11. LSP 集成:
    - 语言服务器协议接入
    - 代码智能重构、跳转定义、查找引用

12. Git 感知:
    - 自动 git diff 作为上下文
    - git log 提取最近改动
    - 提交前自动 review

13. Session 持久化:
    - checkpoint/restore 支持
    - 跨重启继续未完成任务
    - LLM 调用缓存减少重复成本

14. 异步核心重构:
    - AsyncOpenAI 替代同步 client
    - 移除 asyncio.to_thread() 包装
    - 真正的并发工具执行
```

---

## 8. SWOT 分析

### NanoGhost

```
┌────────────────────────────┬────────────────────────────┐
│ STRENGTHS                  │ WEAKNESSES                 │
├────────────────────────────┼────────────────────────────┤
│ • 三层记忆系统（独创）       │ • 无代码编辑工具（致命弱点）  │
│ • 多实例 Gateway 运维        │ • 无上下文压缩（长对话崩溃）  │
│ • 飞书深度集成               │ • 同步核心性能低（吞吐受限） │
│ • MCP 生产级实现             │ • 单体代码 bloat            │
│ • PyInstaller 一键部署       │ • 线程安全问题              │
│ • SKILL.md 生态兼容          │ • 测试覆盖率 ~15%           │
│ • 端口-适配器 DI 架构         │ • 无 Agent Teams            │
│ • 通道抽象（易于扩展）        │ • 无任务持久化              │
├────────────────────────────┼────────────────────────────┤
│ OPPORTUNITIES              │ THREATS                    │
├────────────────────────────┼────────────────────────────┤
│ • 学习 l-c-c 补齐编码工具    │ • 新 agent 框架快速迭代     │
│ • 增加 agent teams 协作     │ • 大模型内置工具调用能力增强 │
│ • 扩展更多 IM 通道           │ • OpenAI/Anthropic 官方 SDK  │
│   （企微/钉钉/Slack/Discord）│   框架挤压中间件空间         │
│ • 作为 Kode SDK 底层引擎     │ • Claude Code/Cursor 等     │
│ • 增加 RAG 代码库索引        │   产品级工具碾压            │
│ • 开源社区化                 │ • 单点维护风险              │
└────────────────────────────┴────────────────────────────┘
```

### learn-claude-code（作为参照）

```
┌────────────────────────────┬────────────────────────────┐
│ STRENGTHS                  │ WEAKNESSES                 │
├────────────────────────────┼────────────────────────────┤
│ • 极简核心循环（永恒不变）    │ • 无通道抽象（仅 CLI）        │
│ • 20 课渐进式教学            │ • 无多实例管理              │
│ • 完整功能栈覆盖             │ • 无生产部署设计            │
│ • 函数式简洁                 │ • 记忆系统简化              │
│ • 团队协作（独创）            │ • 无 PyInstaller 打包        │
│ • Worktree 隔离（独创）       │ • 无真实 IM 集成            │
│ • 跨版本 diff 学习            │ • 无 Gateway API            │
│ • 错误恢复机制               │ • 测试同样不足              │
├────────────────────────────┼────────────────────────────┤
│ OPPORTUNITIES              │ THREATS                    │
├────────────────────────────┼────────────────────────────┤
│ • Kode CLI/SDK 产品化       │ • 教育场景天花板有限         │
│ • 作为教学标准               │ • 正式产品需要更多工程投入  │
│ • 多语言翻译覆盖             │ • 社区贡献质量不可控         │
└────────────────────────────┴────────────────────────────┘
```

---

## 9. 综合评分矩阵

| 维度 | NanoGhost | learn-claude-code | 说明 |
|------|:---------:|:-----------------:|------|
| 架构设计 | 8 | 9 | l-c-c 循环不变式哲学更优雅 |
| 代码组织 | 6 | 9 | NanoGhost 单体文件过重 |
| 核心编码能力 | 3 | 9 | **NanoGhost 缺少 grep/glob/edit/write** |
| 上下文管理 | 4 | 8 | NanoGhost 无自动压缩 |
| 任务系统 | 2 | 8 | NanoGhost 无持久化任务/后台执行 |
| 错误恢复 | 3 | 8 | NanoGhost 无 LLM 重试/降级 |
| 记忆系统 | 9 | 5 | NanoGhost 三层记忆远胜 |
| MCP 集成 | 8 | 6 | NanoGhost 更完善（cooldown/去重/白名单） |
| 通道集成 | 9 | 0 | NanoGhost 独有飞书深度集成 |
| 多智能体 | 3 | 8 | NanoGhost 无 Agent Teams |
| 权限安全 | 2 | 7 | NanoGhost 基本无安全机制 |
| 运维部署 | 9 | 2 | NanoGhost Gateway + .exe 远胜 |
| 技能系统 | 8 | 6 | NanoGhost frontmatter + Hermes 更完整 |
| 扩展性 | 7 | 8 | 各有优势 |
| 测试覆盖 | 3 | 4 | 两者均不足 |
| **综合** | **5.6** | **6.5** | 各有所长，NanoGhost 需补齐编码与协作 |

---

## 结论

**NanoGhost** 是面向生产 bot 部署的框架，其最大价值在于运维能力（Gateway 多进程管理、PyInstaller .exe、飞书深度集成）和记忆系统创新（三层记忆 + 图谱 + 经验学习）。但它缺少作为**编码 agent** 的核心能力——grep/glob/edit/write 工具、上下文压缩、任务持久化、Agent Teams。

**learn-claude-code** 的核心教训是：**循环永不变，扩展靠注册**。NanoGhost 可以大幅受益于这种设计哲学——将庞大的 builtins.py 拆为独立工具模块，在 AgentExecutor 周围增加 context compaction、task system、agent teams 等 harness 机制，而无需重写核心循环。

**建议路径**：NanoGhost 作为生产 bot 基座保持不变，逐步补齐 (1) 编码工具 (2) 上下文压缩 (3) 任务系统 (4) Agent Teams，即可成为一个同时具备**生产运维能力 + 编码代理能力**的全功能 agent 平台。
