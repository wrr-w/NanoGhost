# NanoGhost 记忆系统增强方案 v3

> 日期: 2026-05-24
> 状态: 草案v3
> 涉及模块: agent_core.memory.*

---

## 两套记忆的分工

```
memory.md                          Flow Cards + Graph
软记忆，跟流程无关                  硬记忆，跟流程绑定

- 用户偏好（喜欢简洁）              - 步骤模式（A-B-C）
- 个人习惯（喜欢先确认再操作）       - 流程级别的踩坑记录
- 软性经验（通用建议）              - 流程级别的经验总结
                                   - 条件分支提示
```

---

## 第一部分：Flow Cards - 流程 + 踩坑一体

### 1.1 当前 Card 结构

现有 `AgentMemoryCard` 存储 steps、intent_summary、flow_hash 等。
缺少跟流程绑定的踩坑和经验。

### 1.2 改造：Card 新增经验字段

```python
@dataclass
class AgentMemoryCard:
    # ... 现有字段不变 ...

    # 新增
    pitfalls: List[str] = field(default_factory=list)
    """踩坑记录，跟具体步骤绑定的问题"""

    experience_notes: List[str] = field(default_factory=list)
    """经验总结，跟流程相关的经验"""

    branch_hints: Dict[str, str] = field(default_factory=dict)
    """分支提示: 在某个分支点如何选择"""
```

### 1.3 写入时机

当 agent 完成一次流程后：

```python
def enrich_card_with_experience(card, steps, reply, llm):
    """从本次执行提取踩坑和经验"""
    # 1. 检测失败/重试模式
    retry = detect_retry_patterns(steps)
    if retry:
        pitfall = f'步骤{retry["step"]} 可能踩坑: {retry["reason"]}'
        card.pitfalls.append(pitfall)

    # 2. 检测新分支
    branch = detect_novel_branch(card, steps)
    if branch:
        card.branch_hints[branch["step"]] = branch["reason"]

    # 3. 去重
    deduplicate_pitfalls(card)
```

### 1.4 读取时机

`retrieve_similar_flows()` 返回时附带经验信息注入 prompt：

```
[历史相似流程]
1. 创建任务
   步骤: POST /api/tasks -> POST /api/task/settings -> POST /api/task/start
   踩坑: settings 里 timeout 不配默认 30 秒超时
   经验: 创建后记得配 settings，跳过可能报错
```

### 1.5 流程模板聚类

```python
def cluster_flow_patterns(cards) -> dict:
    """按归一化步骤链聚类，聚合踩坑和经验"""
    patterns = {}
    for card in cards:
        sig = tuple(s["method"] + normalize_path(s["path"])
                for s in card.get("steps", []))
        p = patterns.setdefault(sig, {
            "count": 0, "intents": [],
            "pitfalls": set(), "experiences": set()
        })
        p["count"] += 1
        p["intents"].append(card.get("intent_summary"))
        p["pitfalls"].update(card.get("pitfalls", []))
        p["experiences"].update(card.get("experience_notes", []))
    return patterns
```

---

## 第二部分：memory.md - 软记忆

### 2.1 定位

跟具体流程无关的软性信息：

```
## 用户偏好
- 喜欢简洁回复
- 喜欢先确认再操作

## 通用经验
- 涉及修改的操作先问用户确认

## 项目环境
- 主要工作在 Windows 10
```

### 2.2 机制

- 存储：{INSTANCE_DIR}/memory.md，纯 Markdown
- 写入：memory_write tool（LLM 自主判断）
- 注入：assemble_sys_prompt() 读入 system prompt
- 不跟任何流程绑定

---

## 第三部分：两套记忆的协同

```
用户: 帮我创建一个抓取任务
        |
        v
+-------------------------------+
| Prompt 注入                   |
| memory.md:                    |
|   - 喜欢简洁回复              |
|   - 操作前先确认              |
| Flow Cards:                   |
|   - 相似流程: 创建任务         |
|   - 踩坑: settings 超时        |
|   - 经验: 记得配 timeout       |
+-------------------------------+
        |
        v
Agent 执行 -> 完事后自动写入
        |
        v
+-------------------------------+
| Graph: 更新转移计数           |
| Card: 记录踩坑/经验           |
| memory.md: LLM 自主判断       |
+-------------------------------+
```

---

## 实施顺序

| 阶段 | 内容 |
|---|---|
| P0 | Card 新增 pitfalls + experience_notes 字段 + DB migration |
| P0 | 注入 prompt 展示踩坑/经验 |
| P0 | memory.md Tool + 注入 |
| P1 | LLM 辅助提取踩坑（写入 Card）|
| P2 | 流程模板聚类 |
| P3 | 分支检测 + 分支提示注入 |