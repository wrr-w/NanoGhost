# NanoGhost 改进计划 v1.0

> 基于 learn-claude-code 对比分析制定 | 2026-06-08

---

## 目录

- [Phase 0: Bug Fix（立即）](#phase-0-bug-fix立即)
- [Phase 1: 补齐编码能力（1-2周）](#phase-1-补齐编码能力1-2周)
- [Phase 2: 上下文与韧性（2-3周）](#phase-2-上下文与韧性2-3周)
- [Phase 3: 任务与协作（3-4周）](#phase-3-任务与协作3-4周)
- [Phase 4: 架构优化（持续）](#phase-4-架构优化持续)
- [Phase 5: 长期愿景](#phase-5-长期愿景)

---

## Phase 0: Bug Fix（立即）

> 目标：修复已知会导致静默失败的严重 bug

| # | 任务 | 文件 | 改动 | 状态 |
|---|------|------|------|:----:|
| 0.1 | 修复 MCP 初始化 import 路径错误 | `engine/agent.py:82` | `from agent_core.infra.config_loader` → `from agent_core.config` | ✅ |
| 0.2 | ContextBuilder 类级状态改实例级 | `channel/message_context.py` | `_state_bot_keys`、`_state_mentions` 改为实例属性 | ✅ |
| 0.3 | memory.md 文件锁保护并发写入 | `memory/intent.py` | `append_to_memory_md()` 增加 `fcntl`/`msvcrt` 文件锁 | ✅ |
| 0.4 | `_seen_events` 添加 TTL 清理 | `channel/feishu/ws_client.py` | 每条消息处理前清理 >120s 的旧条目 | ✅ |
| 0.5 | Feishu Token Manager 支持多实例 | `channel/feishu/api.py` | TokenManager 改为按 (app_id, app_secret) 键隔离 | ✅ |

---

## Phase 1: 补齐编码能力（1-2周）

> 目标：使 NanoGhost 具备作为编码 agent 的基础能力，对标 learn-claude-code s01-s04

### 1.1 新增核心编码工具

```
src/agent_core/tool/builtins/
├── __init__.py       # 从原 builtins.py 拆分
├── terminal.py       # shell 执行（原有，迁移）
├── read.py           # 文件读取（原有，迁移）
├── write.py          # 文件创建/覆写  ← 新增
├── edit.py           # 精确字符串替换编辑  ← 新增
├── glob.py           # 文件名模式匹配  ← 新增
├── grep.py           # 正则内容搜索  ← 新增
├── skills.py         # skill 相关工具（原有，迁移）
├── memory.py         # memory 相关工具（原有，迁移）
├── delegate.py       # subagent 委派（原有，迁移）
└── ask.py            # ask_user（原有，迁移）
```

### 1.2 工具合约

| 工具 | 输入 | 输出 | 安全约束 |
|------|------|------|----------|
| `write` | `file_path`, `content` | success/error | 限制 workspace 内 |
| `edit` | `file_path`, `old_string`, `new_string`, `replace_all?` | patch result | 限制 workspace 内；old_string 必须唯一匹配 |
| `glob` | `pattern`, `path?` | 匹配文件列表 | 限制 workspace 内 |
| `grep` | `pattern`, `path?`, `include?` | `file:line:content` 列表 | 限制 workspace 内 |

### 1.3 改动清单

| # | 文件 | 说明 |
|---|------|------|
| 1.1 | `tool/builtins/terminal.py` | 从 `builtins.py` 提取 shell 执行逻辑 |
| 1.2 | `tool/builtins/read.py` | 从 `builtins.py` 提取文件读取逻辑 |
| 1.3 | `tool/builtins/write.py` | **新增**：文件写入工具 |
| 1.4 | `tool/builtins/edit.py` | **新增**：字符串替换编辑工具 |
| 1.5 | `tool/builtins/glob.py` | **新增**：文件名模式匹配工具 |
| 1.6 | `tool/builtins/grep.py` | **新增**：正则内容搜索工具 |
| 1.7 | `tool/builtins/skills.py` | 从 `builtins.py` 提取 skills 工具 |
| 1.8 | `tool/builtins/memory.py` | 从 `builtins.py` 提取 memory 工具 |
| 1.9 | `tool/builtins/delegate.py` | 从 `builtins.py` 提取 delegate_task |
| 1.10 | `tool/builtins/ask.py` | 从 `builtins.py` 提取 ask_user |
| 1.11 | `tool/__init__.py` | 从新模块 import，保持原 registry 不变 |
| 1.12 | `tool/builtins.py` | 改为 re-export 新模块，标记 deprecated |
| 1.13 | `prompts/agent_profile.md` | 添加新工具描述 |
| 1.14 | `engine/executor.py` | 增加工具参数 schema 验证 |

---

## Phase 2: 上下文与韧性（2-3周）

> 目标：支持长时间、多轮次工作不崩溃，对标 learn-claude-code s08/s11

### 2.1 上下文压缩（Context Compaction）

```
src/agent_core/engine/
├── compaction.py     ← 新增：3 层压缩策略
```

**策略分层**：

| 层 | 名称 | 触发条件 | 行为 |
|----|------|----------|------|
| L1 | **microCompact** | 每轮自动 | 旧工具结果压缩为 `[tool: xxx, result truncated]` 占位符，保留最近 3 轮完整内容 |
| L2 | **autoCompact** | 预估 token > 阈值（默认 50000） | 写入完整 transcript 到 JSONL 存档，LLM 摘要对话历史，替换活跃上下文 |
| L3 | **manualCompact** | 用户触发 `/compact` 命令 | 同 L2，但由用户手动触发 |

### 2.2 LLM 错误恢复

| 策略 | 行为 |
|------|------|
| **Retry with backoff** | 瞬时错误（429/5xx）指数退避重试（1s/2s/4s/8s），最多 3 次 |
| **Fallback model** | 连续失败后切换备用模型（通过 `LLM_FALLBACK_MODEL` 配置） |
| **Context escalation** | context 超限时自动触发 compact 后重试 |
| **Graceful degradation** | 全部失败后返回结构化错误事件，而非崩溃 |

### 2.3 Token 估算改进

| # | 任务 |
|---|------|
| 2.3.1 | 引入 `tiktoken` 进行精确 token 计数 |
| 2.3.2 | 若 `tiktoken` 不可用，保持 char/3 作为 fallback |
| 2.3.3 | 根据 `LLM_MODEL` 自动选择对应的 encoding |

### 2.4 改动清单

| # | 文件 | 说明 |
|---|------|------|
| 2.1 | `engine/compaction.py` | **新增**：3 层上下文压缩实现 |
| 2.2 | `engine/executor.py` | 集成 compaction 检查点（每轮后检查 threshold） |
| 2.3 | `engine/messages.py` | 支持 microCompact 处理；精确 token 计数 |
| 2.4 | `adapters/llm.py` | 增加重试逻辑（exponential backoff） |
| 2.5 | `config.py` | 新增 `LLMConfig.retry_max`, `.fallback_model`, `.compact_threshold` |
| 2.6 | `config.yaml` | 支持在实例配置中覆盖上述参数 |

---

## Phase 3: 任务与协作（3-4周）

> 目标：支持复杂任务分解、持久化、多 Agent 协作，对标 learn-claude-code s12-s18

### 3.1 任务系统（Task System）

```
src/agent_core/task/
├── __init__.py
├── board.py          # TaskBoard: JSON 持久化 + CRUD + 依赖图
├── models.py         # TaskRecord dataclass
└── background.py     # BackgroundJob: 后台命令执行 + 通知注入
```

**数据结构**：

```python
@dataclass
class TaskRecord:
    id: str
    subject: str
    description: str
    status: str           # pending | in_progress | completed | cancelled
    blocked_by: list[str] # 前置任务 ID 列表
    owner: str            # agent 名称（用于团队协作）
    created_at: float
    updated_at: float
```

### 3.2 Agent Teams

```
src/agent_core/team/
├── __init__.py
├── teammate.py       # Teammate: 命名持久队友
├── mailbox.py        # JSONL 邮箱通信
├── protocol.py       # shutdown handshake + plan approval
└── autoclaim.py      # idle 扫描 + 自动领取任务
```

### 3.3 Worktree 隔离

```
src/agent_core/task/
└── worktree.py       # git worktree 创建/绑定/销毁
```

### 3.4 改动清单

| # | 文件 | 说明 |
|---|------|------|
| 3.1 | `task/__init__.py` | 任务系统入口 |
| 3.2 | `task/models.py` | TaskRecord, BackgroundJob 数据模型 |
| 3.3 | `task/board.py` | TaskBoard CRUD + 依赖解析 |
| 3.4 | `task/background.py` | BackgroundJobManager + 通知注入到 executor |
| 3.5 | `task/worktree.py` | WorktreeManager: Git worktree 生命周期 |
| 3.6 | `team/teammate.py` | Teammate: 角色、system_prompt、生命周期 |
| 3.7 | `team/mailbox.py` | JSONL 消息队列 |
| 3.8 | `team/protocol.py` | 审批/关闭协商 |
| 3.9 | `team/autoclaim.py` | 自动任务领取循环 |
| 3.10 | `engine/agent.py` | 集成 TaskBoard + BackgroundJob，保持原有接口不变 |
| 3.11 | `engine/executor.py` | 任务创建/更新工具注册；后台完成通知注入 |
| 3.12 | `tool/builtins/task.py` | `task_create`, `task_list`, `task_update`, `task_bg_run` |
| 3.13 | `tool/builtins/team.py` | `team_spawn`, `team_message`, `team_broadcast` |
| 3.14 | `config.py` | 新增 team/worktree 相关配置 |
| 3.15 | `prompts/agent_rules_conduct.md` | 添加任务与协作行为规则 |

---

## Phase 4: 架构优化（持续）

> 目标：提升代码质量、可维护性、测试覆盖

### 4.1 代码重构

| # | 任务 | 优先级 |
|---|------|:------:|
| 4.1.1 | 拆分 `builtins.py`（2189行）为独立模块（已在 Phase 1 完成） | P1 |
| 4.1.2 | 拆分 `cli.py`（708行）为 `cli/instance.py`, `cli/gateway.py`, `cli/mcp.py` | P2 |
| 4.1.3 | 拆分 `run.py`（541行）为 `run/bootstrap.py`, `run/cli.py`, `run/feishu.py` | P2 |
| 4.1.4 | 所有模块添加 `__init__.py` 的 `__all__` 导出限定 | P2 |
| 4.1.5 | 统一错误处理：定义 `NanoGhostError` 基类 + 子类 | P2 |

### 4.2 测试覆盖率

| # | 任务 | 目标覆盖率 |
|---|------|:----------:|
| 4.2.1 | `tool/` 工具单元测试 | >80% |
| 4.2.2 | `engine/` 核心循环测试 | >70% |
| 4.2.3 | `memory/` 记忆系统测试 | >70% |
| 4.2.4 | `task/` 任务系统测试 | >80% |
| 4.2.5 | `team/` 团队协作测试 | >70% |
| 4.2.6 | `channel/` 通道测试（mock 飞书 API） | >60% |

### 4.3 性能优化

| # | 任务 |
|---|------|
| 4.3.1 | Memory Cards 支持分页加载，避免 `load_all_memory_cards()` O(n) |
| 4.3.2 | `record_successful_flow()` 改为增量更新而非全量重写 |
| 4.3.3 | Embedding 结果缓存（LRU cache） |
| 4.3.4 | 数据库连接池替代每次操作创建新连接 |

---

## Phase 5: 长期愿景

> 目标：成为生产级全功能 agent 平台

| # | 特性 | 说明 |
|---|------|------|
| 5.1 | **Cron 调度器** | 定时任务触发，agent 可自行管理调度规则 |
| 5.2 | **LSP 集成** | 语言服务器协议接入，智能代码重构/跳转/引用查找 |
| 5.3 | **Git 感知** | 自动 git diff 作为上下文，提交历史感知 |
| 5.4 | **Session 持久化** | checkpoint/restore，跨重启继续任务 |
| 5.5 | **RAG 代码库索引** | 代码库向量索引，语义级代码搜索 |
| 5.6 | **多 IM 通道扩展** | 企微、钉钉、Slack、Discord、Telegram |
| 5.7 | **Web Dashboard** | 实例管理、任务看板、对话历史可视化 |
| 5.8 | **插件市场** | 技能和 MCP Server 一键安装 |
| 5.9 | **权限审批系统** | 结构化审批管道，黑名单+白名单，操作审计日志 |
| 5.10 | **Hooks 扩展** | on_turn_start/end, on_message_received, SessionStart/End |

---

## 进度追踪

| Phase | 状态 | 开始日期 | 完成日期 | 备注 |
|-------|:----:|----------|----------|------|
| Phase 0: Bug Fix | ✅ 已完成 | 2026-06-08 | 2026-06-08 | 5 个 bug 全部修复 |
| Phase 1: 编码能力 | ✅ 已完成 | 2026-06-08 | 2026-06-08 | 拆分 builtins.py + 新增 4 个工具 |
| Phase 2: 上下文韧性 | ⬜ 待开始 | - | - | |
| Phase 3: 任务协作 | ⬜ 待开始 | - | - | |
| Phase 4: 架构优化 | ⬜ 待开始 | - | - | |
| Phase 5: 长期愿景 | ⬜ 待开始 | - | - | |

---

## 设计原则

1. **循环不变式**：所有 harness 机制叠加在 AgentExecutor 循环之上，不修改循环本体
2. **工具可注册**：新增能力 = 定义 schema + 注册 handler，dispatch map 驱动
3. **向后兼容**：新功能通过 opt-in 配置开启，现有 bot 实例不受影响
4. **渐进交付**：每个 Phase 独立可发布，不依赖后续 Phase
