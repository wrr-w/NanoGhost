# AGENTS.md — 改动日志

## 2026-06-20 — 用户身份查询系统

### 背景
- 私聊时 bot 不知道对方是谁（sender_name 为空时只显示 "用户"）
- 群聊时 200 人全量成员名 dump 到 system prompt，不合理且低效
- Bot 无法按需查询用户身份（部门、职位等）

### 改动

| # | 文件 | 改动 |
|---|------|------|
| 1 | `src/agent_core/channel/feishu/api.py` | 新增 `get_user_info(open_id)` — 调用飞书 contact/v3/users/ 返回姓名、职位、邮箱、部门等完整信息 |
| 2 | `src/agent_core/presenter.py` | 删除 `get_chat_members` 全部 dump（群成员列表不再硬塞 system prompt） |
| 3 | `src/agent_core/channel/feishu/tools.py` | **新增**，飞书特有工具层：`_lookup_user_handler` + `register_feishu_tools()`，通过模块级 `_mention_map` 持有 name→open_id 映射引用 |
| 4 | `src/agent_core/channel/feishu/ws_client.py` | 去掉内联工具注册和 handler，改为 `from .tools import register_feishu_tools; register_feishu_tools(...)` 一行调用 |
| 5 | `src/agent_core/channel/message_context.py` | `MessageSource.display_name` fallback 改为显示 open_id 后 8 位（而非 "用户"）；`build_session_context()` 私聊时展示 `用户: name (ID: ou_xxx)` 让 bot 有 open_id 可查 |

### 效果
- 私聊：bot 看到 `**用户:** 张三 (ID: ou_xxx)` + `[张三] 消息`，不确定时可调用 `lookup_user` 查详情
- 群聊：不再输出 200 人列表到 prompt，bot 看到 `[张三] 消息`，需要时自行 `lookup_user("张三")`
- `lookup_user` 支持名称模糊匹配，也支持直接传 open_id
