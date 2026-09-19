from typing import Any

from .ask import ASK_USER_DEF, ask_user
from .delegate import DELEGATE_TASK_DEF, delegate_task
from .edit import EDIT_DEF, edit_file
from .endpoints import LIST_ENDPOINTS_DEF, list_endpoints
from .glob import GLOB_DEF, glob_files
from .grep import GREP_DEF, grep_search
from .memory import (
    GET_CARD_DETAIL_DEF,
    LIST_CARDS_DEF,
    MEMORY_EXPLORE_DEF,
    MEMORY_READ_DEF,
    MEMORY_WRITE_DEF,
    handle_get_card_detail,
    handle_list_cards,
    memory_explore,
    memory_read,
    memory_write,
)
from .read import READ_DEF, read_file
from .send import SEND_MESSAGE_DEF, send_message
from .skills import (
    SKILL_INSTALL_DEF,
    SKILL_MANAGE_DEF,
    SKILLS_LIST_DEF,
    USE_SKILL_DEF,
    _has_npx,
    _has_skills_dir,
    skill_install,
    skill_manage,
    skills_list,
    use_skill,
)
from .tasks import (
    CANCEL_SCHEDULED_TASK_DEF,
    LIST_SCHEDULED_TASKS_DEF,
    SCHEDULE_TASK_DEF,
    cancel_scheduled_task,
    list_scheduled_tasks,
    schedule_task,
)
from .terminal import TERMINAL_DEF, terminal
from .write import WRITE_DEF, write_file


def register_builtins(registry: Any) -> None:
    """Register all built-in tools into a ToolRegistry instance."""

    registry.register("terminal", terminal,
                      description="在本地终端执行 shell 命令。执行代码、运行脚本、访问文件系统时使用。",
                      parameters=TERMINAL_DEF, category="system")

    registry.register("read", read_file,
                      description="读取本地文件内容。"
                                  "读取 SKILL.md 引用的 references/ 或 scripts/ 文件时使用。"
                                  "path 必须是绝对路径。",
                      parameters=READ_DEF, category="system")

    registry.register("write", write_file,
                      description="创建/覆写文件。file_path 为绝对路径，content 为文件内容。",
                      parameters=WRITE_DEF, category="system")

    registry.register("edit", edit_file,
                      description="精确字符串替换编辑文件。old_string 必须唯一匹配（或设置 replace_all=true 替换全部）。",
                      parameters=EDIT_DEF, category="system")

    registry.register("glob", glob_files,
                      description="按文件名模式搜索文件。pattern 支持 glob 语法如 **/*.py。",
                      parameters=GLOB_DEF, category="system")

    registry.register("grep", grep_search,
                      description="正则表达式搜索文件内容。返回 file:line:content 格式。include 可限定文件类型如 *.py。",
                      parameters=GREP_DEF, category="system")

    registry.register("ask_user", ask_user,
                      description="向用户提问并等待回答。当需要用户确认或选择时使用。",
                      parameters=ASK_USER_DEF, category="system")

    registry.register("use_skill", use_skill,
                      description="加载一个可用技能（SKILL.md）的完整指示并注入到对话中。"
                                  "技能包含特定任务的详细指令和 API 信息。",
                      parameters=USE_SKILL_DEF, category="skill",
                      check_fn=_has_skills_dir)

    registry.register("skills_list", skills_list,
                      description="列出所有可用的技能名称和描述。" + (" " * 50),
                      parameters=SKILLS_LIST_DEF, category="skill",
                      check_fn=_has_skills_dir)

    registry.register("skill_manage", skill_manage,
                      description="管理技能：创建、修改、删除技能及其支持文件。"
                                  "技能文件存储在 ~/.agents/skills/<name>/ 目录下。",
                      parameters=SKILL_MANAGE_DEF, category="skill",
                      check_fn=_has_skills_dir)

    registry.register("skill_install", skill_install,
                      description="从生态安装一个技能包。"
                                  "安装后自动重新发现技能。使用前可先用 skills_list 查看可用技能。",
                      parameters=SKILL_INSTALL_DEF, category="skill",
                      check_fn=lambda: _has_skills_dir() and _has_npx())

    registry.register("memory_write", memory_write,
                      description="Write/update/delete entries in memory.md.",
                      parameters=MEMORY_WRITE_DEF, category="system")
    registry.register("memory_explore", memory_explore,
                      description="Query multi-layer operation graph. action=node for L1+L2 view, action=drill for L3/L4 detail.",
                      parameters=MEMORY_EXPLORE_DEF, category="system")
    registry.register("memory_read", memory_read,
                      description="Read memory.md with layered disclosure: index, section, detail.",
                      parameters=MEMORY_READ_DEF, category="system")
    registry.register("list_cards", handle_list_cards,
                      description="Browse card index. Optional domain filter by L1 code.",
                      parameters=LIST_CARDS_DEF, category="system")
    registry.register("get_card_detail", handle_get_card_detail,
                      description="Expand one card by flow_hash to see full steps + experience.",
                      parameters=GET_CARD_DETAIL_DEF, category="system")
    registry.register("delegate_task", delegate_task,
                      description="Delegate task to sub-agent.",
                      parameters=DELEGATE_TASK_DEF, category="subagent")

    # ── 定时任务（agent 自助）────────────────────────────
    registry.register("schedule_task", schedule_task,
                      description="创建定时任务：让 agent 在指定时刻自动执行一段指令。"
                                  "支持 cron（『0 3 * * *』=每天3点）、daily_at（『15:00』）、"
                                  "interval（每N秒）、at（一次性）。"
                                  "例：用户说『每天3点校验工单』→ cron='0 3 * * *'。"
                                  "创建后无需重启，最长5秒生效。",
                      parameters=SCHEDULE_TASK_DEF, category="task")

    registry.register("list_scheduled_tasks", list_scheduled_tasks,
                      description="列出当前所有定时任务（含调度方式、下次执行时间、已执行次数）。",
                      parameters=LIST_SCHEDULED_TASKS_DEF, category="task")

    registry.register("cancel_scheduled_task", cancel_scheduled_task,
                      description="取消一个定时任务（按 name 或 id）。",
                      parameters=CANCEL_SCHEDULED_TASK_DEF, category="task")

    # ── 出站（P2）：模型自由路由 ─────────────────────────
    registry.register("send_message", send_message,
                      description="把消息发到指定端点（不填 to = 回当前会话）。"
                                  "to 可填多个地址（'channel:target'，如 feishu:oc_xxx）实现一对多。",
                      parameters=SEND_MESSAGE_DEF, category="channel")

    # ── 通道管理（P4）：看端点菜单 ───────────────────────
    registry.register("list_endpoints", list_endpoints,
                      description="列出可发送的端点（地址/类别/状态/能力）+ 各通道概览。"
                                  "用于决定 send_message 的 to。",
                      parameters=LIST_ENDPOINTS_DEF, category="channel")
