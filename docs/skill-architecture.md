# Skill 架构：树状递归技能系统

## 一、设计思想

技能按**树状层级**组织，每层是一个独立的 SKILL.md，包含：
- **schema**（frontmatter）：name + description，供 LLM 索引判断
- **内容**：什么时候用、子技能列表、使用方式

LLM 从顶层开始，按需逐层递归下钻，不一次性加载全部内容。

## 二、目录结构

```
~/.agents/skills/                      ← 技能根目录
│
├── lark/                              ← 分组目录（第一层）
│   ├── SKILL.md                       ← 分组 schema：描述 + 子技能表格
│   ├── lark-im/
│   │   └── SKILL.md                   ← 子技能（第二层）：完整指令
│   ├── lark-calendar/
│   │   └── SKILL.md
│   ├── lark-base/
│   │   └── SKILL.md
│   └── ...                            ← 其他 lark-* 技能
│
├── image/                             ← 分组目录（第一层）
│   ├── SKILL.md                       ← 分组 schema
│   ├── image-utils/
│   │   └── SKILL.md
│   └── img-to-base64/
│       └── SKILL.md
│
├── anysearch/                         ← 平铺技能（无子技能）
│   └── SKILL.md
│
└── local-search/
    └── SKILL.md
```

### 三种节点类型

| 类型 | 目录特征 | 示例 |
|------|----------|------|
| **分组** | 有 SKILL.md + 含子技能目录 | `lark/` 下有 `lark-im/`、`lark-calendar/` 等 |
| **隐式分组** | 无 SKILL.md + 含子技能目录 | 目录名自动作为分组名 |
| **平铺技能** | 有 SKILL.md + 无子技能目录 | `anysearch/`、`local-search/` |

## 三、每层 schema 格式

### 分组层 SKILL.md

```markdown
---
name: lark
description: 飞书 / Lark 全能力集成 — 消息、日历、文档、审批、OKR、通讯录等
---

## 飞书技能集

### 什么时候用
- 发送/接收消息
- 创建日历日程
- 管理文档、表格
- ...

### 包含的子技能

| 技能 | 用途 | 使用场景 |
|------|------|----------|
| lark-im | 即时通讯 | 发消息、查聊天记录 |
| lark-calendar | 日历 | 创建会议、约会议室 |
| ... | ... | ... |

### 使用方式
use_skill(name="lark-im")
```

### 子技能层 SKILL.md

```markdown
---
name: lark-im
description: 飞书即时通讯 — 收发消息和管理群聊
---

## 即时通讯 (IM)

### 什么时候用
- 需要发送消息到群聊或私聊
- 需要查看/搜索聊天记录
- 需要管理群成员

### 核心指令
发消息: lark-cli im +messages-send ...

### 参考文件
use_skill(name="lark-im", file_path="references/xxx.md")
```

## 四、加载链路

```
┌─────────────────────────────────────────────────────┐
│ ① 启动：全量扫描磁盘，加载到内存                     │
│ Agent.__init__()                                     │
│   → SkillRegistry.discover()                         │
│   → 递归扫描 ~/.agents/skills/**/SKILL.md            │
│   → 解析 frontmatter + 全文 → Dict[name, SkillDef]   │
│   内存中有全部内容，但不发给 LLM                      │
└─────────────────────────────────────────────────────┘
         ↓
┌─────────────────────────────────────────────────────┐
│ ② 每轮对话：注入第一层索引到 system prompt           │
│ AgentExecutor.run()                                  │
│   → build_skill_context()                            │
│   → 只提取：分组名 + 一句话描述                      │
│   → 按 <available_skills> 格式注入                   │
│   给 LLM 看的是：                                     │
│   📁 lark: 飞书能力集成  use_skill(name="lark")      │
│   📁 image: 图片工具集    use_skill(name="image")    │
│   📄 anysearch: 搜索引擎                              │
└─────────────────────────────────────────────────────┘
         ↓  LLM 判断需要飞书，调 use_skill("lark")
┌─────────────────────────────────────────────────────┐
│ ③ 按需加载：use_skill 工具从内存取出全文注入对话      │
│ LLM 看到 lark 分组 SKILL.md：                         │
│   - 什么时候用                                       │
│   - 子技能表格（含用途、场景）                        │
│   - 使用方式                                         │
└─────────────────────────────────────────────────────┘
         ↓  LLM 判断需要发消息，调 use_skill("lark-im")
┌─────────────────────────────────────────────────────┐
│ ④ 再下钻：加载具体子技能的完整指令                    │
│ LLM 看到 lark-im 完整内容：                           │
│   - 核心命令                                         │
│   - 参数说明                                         │
│   - 参考文件链接                                     │
└─────────────────────────────────────────────────────┘
         ↓  执行工具完成任务
```

## 五、递归特性

- 理论上支持**无限层级**：分组下可以再有分组
- 每层的 SKILL.md 格式**完全一致**（frontmatter + 描述 + 子节点表格 + 使用方式）
- LLM 通过统一的 `use_skill(name)` 接口逐层下钻
- 不限制层数，但实际 2-3 层足够

## 六、关键代码文件

| 文件 | 职责 |
|------|------|
| `src/agent_core/skill/models.py` | SkillDefinition（单节点）、SkillGroup（分组） |
| `src/agent_core/skill/discovery.py` | 递归扫描磁盘、解析 frontmatter |
| `src/agent_core/skill/registry.py` | 内存注册表、树状索引生成 |
| `src/agent_core/tool/builtins/skills.py` | use_skill / skills_list 工具 |
| `src/agent_core/engine/agent.py:315-324` | 每轮注入 skill 索引到 system prompt |

## 七、Token 优化

| 阶段 | 加载内容 | Token 成本 |
|------|----------|-----------|
| 每轮 system prompt | 分组名 + 一句话描述 | ~50 tokens（30个技能） |
| use_skill("分组名") | 分组 SKILL.md（子技能表格） | ~500 tokens |
| use_skill("子技能名") | 完整技能指令 + references | ~2000 tokens |

仅当 LLM 明确需要时才加载完整内容，避免浪费。
