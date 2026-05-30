"""Built-in tool handlers for the agent.

Each handler is a callable ``(args: dict, ctx: dict) -> ToolResult``.
The context dict contains:
    - db, llm, http, image_port — port instances
    - config — AgentConfig
    - agent — Agent instance
    - session_id, namespace
    - step_counter (int, mutable via closure)
    - all_steps_out (list)
    - user_message (str)
    - yield_event (callable) — yields (type, data) event tuples
"""

import json
import logging
import os
import re
from typing import Any, Dict, Optional

from agent_core.engine.config import AgentConfig
from agent_core.engine.executor import _execute_shell_command
from .models import ToolResult

logger = logging.getLogger("agent_core")


# ---------------------------------------------------------------------------
# terminal — shell command execution
# ---------------------------------------------------------------------------
# terminal — shell command execution
# ---------------------------------------------------------------------------

def terminal(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """Execute a shell command."""
    command = args.get("command") or args.get("path") or ""
    timeout = args.get("timeout", 120)
    workdir = args.get("workdir")
    step_counter = ctx["step_counter"]
    all_steps_out = ctx["all_steps_out"]
    config = ctx.get("config")

    if workdir is None and config:
        workdir = getattr(config, "shell_cwd", None)
    if timeout == 120 and config:
        timeout = getattr(config, "shell_timeout", 120)

    yield_event = ctx.get("yield_event")
    if yield_event:
        yield_event("step_start", {"step": step_counter[0] + 1, "method": "EXEC", "path": command})

    step_out, ok, error = _execute_shell_command(
        command=command,
        step_num=step_counter[0] + 1,
        timeout=timeout,
        workdir=workdir,
    )

    step_counter[0] += 1
    all_steps_out.append(step_out)

    if yield_event:
        result_summary = {
            k: v for k, v in step_out.items()
            if k in ("ok", "exit_code", "error") and v is not None
        }
        yield_event("step_done", {
            "step": step_counter[0], "ok": ok, "path": command,
            "error": error, "result": result_summary,
        })

    obs = step_out.get("result_preview") or ""
    if error:
        obs = f"命令失败: {error}\n{obs}"
    obs += "\n\n请决定下一步动作。"
    return ToolResult(ok=ok, data=obs, error=error, signal="__continue__")


TERMINAL_DEF = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "要执行的 shell 命令",
        },
        "timeout": {
            "type": "integer",
            "description": "超时秒数（默认 120）",
        },
        "workdir": {
            "type": "string",
            "description": "工作目录（可选）",
        },
    },
    "required": ["command"],
}


# ---------------------------------------------------------------------------
# read — read a file from the filesystem
# ---------------------------------------------------------------------------

def read_file(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """Read a file from the local filesystem."""
    path = (args.get("path") or "").strip()
    if not path:
        return ToolResult(ok=False, error="缺少 path 参数")

    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        return ToolResult(ok=True, data=content)
    except FileNotFoundError:
        return ToolResult(ok=False, error=f"文件不存在: {path}")
    except Exception as e:
        return ToolResult(ok=False, error=f"读取失败: {e}")


READ_DEF = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "文件的绝对路径",
        },
    },
    "required": ["path"],
}


# ---------------------------------------------------------------------------
# use_skill — load a skill content
# ---------------------------------------------------------------------------

# Template variable pattern: ${VAR_NAME}
_SKILL_TEMPLATE_RE = re.compile(r"\$\{(HERMES_SKILL_DIR|HERMES_SESSION_ID)\}")


def _substitute_template_vars(text: str, skill_dir: str, session_id: str = "") -> str:
    """替换 skill 内容中的模板变量，与 Hermes 兼容。"""
    def _replace(m: re.Match) -> str:
        token = m.group(1)
        if token == "HERMES_SKILL_DIR":
            return skill_dir
        if token == "HERMES_SESSION_ID":
            return session_id or m.group(0)
        return m.group(0)
    return _SKILL_TEMPLATE_RE.sub(_replace, text)


def use_skill(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """Load a skill's SKILL.md, or read a supporting file within it."""
    skill_name = (args.get("name") or args.get("skill") or "").strip()
    if not skill_name:
        return ToolResult(ok=False, error="缺少技能名称")

    agent = ctx.get("agent")
    if agent is None:
        return ToolResult(ok=False, error="Agent 不可用")

    sd = agent.skill_registry.get_skill_def(skill_name)

    # 首次未找到时尝试重新发现
    if sd is None:
        try:
            agent.discover_skills()
        except Exception:
            pass
        sd = agent.skill_registry.get_skill_def(skill_name)

    yield_event = ctx.get("yield_event")

    if sd is None:
        available = ", ".join(agent.skill_registry.all_skill_names())
        return ToolResult(
            ok=False,
            error=f"技能 [{skill_name}] 不存在。可用: {available}",
            signal="__continue__",
        )

    skill_dir = os.path.dirname(sd.filepath)
    session_id = ctx.get("session_id", "")
    file_path = args.get("file_path", "").strip()

    # -- file_path mode: read a supporting file relative to skill_dir --
    if file_path:
        if ".." in file_path.replace("\\", "/").split("/"):
            return ToolResult(ok=False, error=f"路径遍历（..）不允许: {file_path}")

        target = os.path.normpath(os.path.join(skill_dir, file_path))
        if not target.startswith(os.path.normpath(skill_dir) + os.sep) and target != os.path.normpath(skill_dir):
            return ToolResult(ok=False, error=f"路径超出了技能目录: {target}")

        try:
            with open(target, encoding="utf-8") as f:
                file_content = f.read()
        except FileNotFoundError:
            return ToolResult(ok=False, error=f"文件不存在: {target}", signal="__continue__")
        except Exception as e:
            return ToolResult(ok=False, error=f"读取失败: {e}", signal="__continue__")

        file_content = _substitute_template_vars(file_content, skill_dir, session_id)
        return ToolResult(
            ok=True,
            data=f"技能 [{skill_name}] 文件: {file_path}\n\n{file_content}",
            signal="__continue__",
        )

    # -- normal mode: load SKILL.md content --
    raw_content = (
        f"## 技能: {sd.name}\n"
        f"{sd.description}\n\n"
        f"{sd.content}\n"
    )
    content = _substitute_template_vars(raw_content, skill_dir, session_id)

    if yield_event:
        yield_event("skill_loaded", {"name": skill_name})

    return ToolResult(
        ok=True,
        data=(
            f"技能 [{skill_name}] 已加载。\n"
            f"技能目录: {skill_dir}\n"
            f"SKILL.md: {sd.filepath}\n\n"
            f"--- SKILL.md 内容 ---\n\n"
            f"{content}\n"
            f"---\n\n"
            f"SKILL.md 中引用的 references/ 和 scripts/ 等文件都是相对「技能目录」的路径。\n"
            f"可用 use_skill(name=\"{skill_name}\", file_path=\"references/xxx.md\") 读取。\n"
            f"或用 read 工具传绝对路径读取。\n"
            f"请根据技能指示继续。"
        ),
        signal="__continue__",
    )


USE_SKILL_DEF = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "技能名称（如 lark-calendar）",
        },
        "file_path": {
            "type": "string",
            "description": "可选：技能目录内的相对路径（如 references/workflow.md），读取该文件而不返回 SKILL.md",
        },
    },
    "required": ["name"],
}


# ---------------------------------------------------------------------------
# skills_list — list available skills
# ---------------------------------------------------------------------------

def skills_list(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """List all available skills (name + description only)."""
    agent = ctx.get("agent")
    if agent is None:
        return ToolResult(ok=False, error="Agent 不可用")

    # 每次调用重新发现，支持运行时新增的技能目录
    try:
        agent.discover_skills()
    except Exception:
        pass

    defs = agent.skill_registry.list_skill_defs()
    if not defs:
        return ToolResult(ok=True, data="(无可用技能)")

    lines = [f"可用技能 ({len(defs)}):"]
    for s in defs:
        lines.append(f"  - {s.name}: {s.description}")
    return ToolResult(ok=True, data="\n".join(lines))


SKILLS_LIST_DEF = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "可选关键字筛选",
        },
    },
}


# ---------------------------------------------------------------------------
# skill_install — install a skill from the ecosystem via npx skills add
# ---------------------------------------------------------------------------

def skill_install(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """Install a skill package from the ecosystem using npx skills add."""
    package = (args.get("package") or "").strip()
    if not package:
        return ToolResult(ok=False, error="缺少 package 参数（格式: owner/repo）")

    import subprocess

    try:
        result = subprocess.run(
            ["npx", "--yes", "skills", "add", package],
            capture_output=True, text=True, timeout=120,
        )
        output = (result.stdout or "") + (result.stderr or "")
    except FileNotFoundError:
        return ToolResult(ok=False, error="未找到 npx，请确保已安装 Node.js")
    except Exception as e:
        return ToolResult(ok=False, error=f"安装失败: {e}")

    agent = ctx.get("agent")
    if agent is not None and result.returncode == 0:
        try:
            new_count = agent.discover_skills()
            if new_count > 0:
                output += f"\n已发现 {new_count} 个新技能"
        except Exception as e:
            output += f"\n技能重新发现警告: {e}"

    success = result.returncode == 0
    return ToolResult(
        ok=success,
        data=output if success else f"安装失败:\n{output}",
        error=None if success else output,
        signal="__continue__",
    )


SKILL_INSTALL_DEF = {
    "type": "object",
    "properties": {
        "package": {
            "type": "string",
            "description": "技能包名称（格式: owner/repo，如 opencode/skills-lark）",
        },
    },
    "required": ["package"],
}


# ---------------------------------------------------------------------------
# delegate_task — spawn a subagent for a specific task
# ---------------------------------------------------------------------------

# Subagent type presets
_SUBAGENT_PRESETS: Dict[str, Dict[str, Any]] = {
    "general": {
        "description": "通用子代理，可执行多步任务、搜索代码、读写文件",
        "tools": None,
    },
    "explore": {
        "description": "快速只读探索，用于搜索代码库、查找文件、回答问题",
        "tools": ["terminal", "skills_list", "use_skill", "read"],
    },
}

# Tools blocked for all subagents (prevent recursion / conflict)
_BLOCKED_SUBAGENT_TOOLS = {"delegate_task", "ask_user", "skill_install"}


def delegate_task(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    sub_type = (args.get("subagent_type") or "general").strip()
    prompt = (args.get("prompt") or "").strip()
    description = (args.get("description") or prompt[:50]).strip()
    run_in_background = args.get("run_in_background", False)

    if not prompt:
        return ToolResult(ok=False, error="缺少 prompt 参数")

    agent = ctx.get("agent")
    if agent is None:
        return ToolResult(ok=False, error="Agent 不可用")

    # Depth limit check (Hermes default max depth = 3)
    delegate_depth = ctx.get("delegate_depth", 0)
    if delegate_depth >= 2:
        return ToolResult(
            ok=False,
            error=f"子代理嵌套深度已达上限 ({delegate_depth})，不能再创建子代理",
            signal="__continue__",
        )

    preset = _SUBAGENT_PRESETS.get(sub_type)
    if preset is None:
        available = ", ".join(_SUBAGENT_PRESETS.keys())
        return ToolResult(ok=False, error=f"未知子代理类型: {sub_type}。可用: {available}")

    yield_event = ctx.get("yield_event")
    config = ctx.get("config")

    import uuid
    sub_name = f"sub_{uuid.uuid4().hex[:8]}"

    # Isolated system prompt — do NOT include subagent tools in the child's scope
    sub_sys_prompt = (
        f"你是一个专门的 {sub_type} 子代理。任务: {description}\n\n"
        f"{config.sys_prompt if config else ''}"
    )

    # Inherit credentials + model from parent
    sub_config = AgentConfig(
        base_url=config.base_url if config else "",
        sys_prompt=sub_sys_prompt,
        api_spec=config.api_spec if config else {},
        verbose=config.verbose if config else False,
    )

    sub = agent.create_sub_agent(sub_name)

    # 1) Apply preset tool restrictions
    if preset.get("tools") is not None:
        allowed = set(preset["tools"])
        for tname in list(sub.tool_registry.list_tools()):
            if tname not in allowed:
                sub.tool_registry.unregister(tname)

    # 2) Always block dangerous / recursive tools
    for tname in _BLOCKED_SUBAGENT_TOOLS:
        try:
            sub.tool_registry.unregister(tname)
        except Exception:
            pass

    child_ctx = {**ctx, "delegate_depth": delegate_depth + 1}

    if yield_event:
        yield_event("subagent_start", {
            "name": sub_name, "type": sub_type, "description": description,
        })

    if run_in_background:
        import threading as _sub_threading
        _bg_results: Dict[str, Any] = {}

        def _run():
            import asyncio as _sub_asyncio
            try:
                async def _run_bg():
                    _reply = ""
                    async for _ev_type, _ev_data in sub.chat_stream_events(
                        user_message=prompt, session_id=None, config=sub_config,
                    ):
                        if _ev_type == "done":
                            _reply = _ev_data.get("reply", "")
                    return _reply
                _bg_results["reply"] = _sub_asyncio.run(_run_bg())
            except Exception as e:
                _bg_results["error"] = str(e)

        t = _sub_threading.Thread(target=_run, daemon=True, name=sub_name)

        t = threading.Thread(target=_run, daemon=True, name=sub_name)
        t.start()
        return ToolResult(
            ok=True, data=f"后台子任务 [{description}] 已启动 (ID: {sub_name})",
            signal="__continue__",
        )

    # Synchronous: collect result (runs sub-agent async loop in a temporary event loop)
    import asyncio as _asyncio_for_sub

    async def _run_sub_agent():
        _reply = ""
        async for _ev_type, _ev_data in sub.chat_stream_events(
            user_message=prompt, session_id=None, config=sub_config,
        ):
            if _ev_type == "text_stream" and yield_event:
                yield_event("subagent_text", {
                    "name": sub_name, "content": _ev_data.get("content", ""),
                })
            elif _ev_type == "done":
                _reply = _ev_data.get("reply", "")
        return _reply

    final_reply = _asyncio_for_sub.run(_run_sub_agent())

    if yield_event:
        yield_event("subagent_result", {
            "name": sub_name, "type": sub_type, "reply": final_reply[:500],
        })

    return ToolResult(
        ok=True,
        data=f"子任务 [{description}] 完成:\n{final_reply}",
        signal="__continue__",
    )


DELEGATE_TASK_DEF = {
    "type": "object",
    "properties": {
        "subagent_type": {
            "type": "string",
            "description": "子代理类型: general（通用，默认）或 explore（只读探索）",
            "enum": ["general", "explore"],
        },
        "description": {
            "type": "string",
            "description": "简短的任务描述（3-5 个词）",
        },
        "prompt": {
            "type": "string",
            "description": "子代理的详细指令",
        },
        "run_in_background": {
            "type": "boolean",
            "description": "是否在后台运行（父代理不等待结果）",
        },
    },
    "required": ["prompt"],
}


# ---------------------------------------------------------------------------
# ask_user — pause and ask the user a question
# ---------------------------------------------------------------------------

def ask_user(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """Ask the user a question and wait for response."""
    question = args.get("question") or "请确认"
    options = args.get("options") or []

    yield_event = ctx.get("yield_event")
    session_id = ctx.get("session_id")

    if yield_event:
        payload = {"question": question, "options": options, "session_id": session_id}
        yield_event("ask_user", payload)

    return ToolResult(ok=True, data=question, signal="__ask__")


ASK_USER_DEF = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "向用户提出的问题"},
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "可选答案列表",
        },
    },
    "required": ["question"],
}


# ---------------------------------------------------------------------------
# skill_manage — create, patch, delete skills and their files
# ---------------------------------------------------------------------------

import shutil
from agent_core.skill.discovery import AGENTS_SKILLS_DIR


def skill_manage(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """Manage skills: create, patch, delete, write/remove supporting files."""
    action = (args.get("action") or "").strip()
    name = (args.get("name") or "").strip()
    if not action or not name:
        return ToolResult(ok=False, error="缺少 action 或 name 参数")

    skill_dir = os.path.join(AGENTS_SKILLS_DIR, name)
    skill_md = os.path.join(skill_dir, "SKILL.md")
    agent = ctx.get("agent")

    # ---- create ----
    if action == "create":
        if os.path.isdir(skill_dir) and os.path.isfile(skill_md):
            return ToolResult(ok=False, error=f"技能 [{name}] 已存在", signal="__continue__")

        content = (args.get("content") or "").strip()
        if not content:
            return ToolResult(ok=False, error="缺少 content 参数")

        os.makedirs(skill_dir, exist_ok=True)
        with open(skill_md, "w", encoding="utf-8") as f:
            f.write(content)

        if agent:
            try:
                agent.discover_skills()
            except Exception:
                pass

        return ToolResult(ok=True, data=f"技能 [{name}] 已创建: {skill_md}", signal="__continue__")

    # ---- patch (find/replace in SKILL.md) ----
    if action == "patch":
        if not os.path.isfile(skill_md):
            return ToolResult(ok=False, error=f"技能 [{name}] 不存在", signal="__continue__")

        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        replace_all = args.get("replace_all", False)

        with open(skill_md, encoding="utf-8") as f:
            content = f.read()

        if replace_all:
            if old_string not in content:
                return ToolResult(ok=False, error=f"未找到匹配文本: {old_string[:60]}", signal="__continue__")
            new_content = content.replace(old_string, new_string)
        else:
            idx = content.find(old_string)
            if idx == -1:
                return ToolResult(ok=False, error=f"未找到匹配文本: {old_string[:60]}", signal="__continue__")
            new_content = content[:idx] + new_string + content[idx + len(old_string):]

        with open(skill_md, "w", encoding="utf-8") as f:
            f.write(new_content)

        if agent:
            try:
                agent.discover_skills()
            except Exception:
                pass

        return ToolResult(ok=True, data=f"技能 [{name}] 已更新", signal="__continue__")

    # ---- delete ----
    if action == "delete":
        if not os.path.isdir(skill_dir):
            return ToolResult(ok=False, error=f"技能 [{name}] 不存在", signal="__continue__")

        import shutil
        shutil.rmtree(skill_dir)

        if agent:
            try:
                agent.discover_skills()
            except Exception:
                pass

        return ToolResult(ok=True, data=f"技能 [{name}] 已删除", signal="__continue__")

    # ---- write_file ----
    if action == "write_file":
        if not os.path.isdir(skill_dir):
            return ToolResult(ok=False, error=f"技能 [{name}] 不存在，请先 create", signal="__continue__")

        file_path = (args.get("file_path") or "").strip()
        file_content = (args.get("file_content") or "").strip()
        if not file_path:
            return ToolResult(ok=False, error="缺少 file_path 参数")

        target = os.path.normpath(os.path.join(skill_dir, file_path))
        if not target.startswith(os.path.normpath(skill_dir) + os.sep):
            return ToolResult(ok=False, error=f"路径超出了技能目录: {file_path}")

        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(file_content)

        return ToolResult(ok=True, data=f"文件已写入: {target}", signal="__continue__")

    # ---- remove_file ----
    if action == "remove_file":
        if not os.path.isdir(skill_dir):
            return ToolResult(ok=False, error=f"技能 [{name}] 不存在", signal="__continue__")

        file_path = (args.get("file_path") or "").strip()
        if not file_path:
            return ToolResult(ok=False, error="缺少 file_path 参数")

        target = os.path.normpath(os.path.join(skill_dir, file_path))
        if not target.startswith(os.path.normpath(skill_dir) + os.sep):
            return ToolResult(ok=False, error=f"路径超出了技能目录: {file_path}")

        if not os.path.isfile(target):
            return ToolResult(ok=False, error=f"文件不存在: {file_path}", signal="__continue__")

        os.remove(target)
        return ToolResult(ok=True, data=f"文件已删除: {target}", signal="__continue__")

    return ToolResult(ok=False, error=f"未知 action: {action}（可选: create/patch/delete/write_file/remove_file）",
                      signal="__continue__")


SKILL_MANAGE_DEF = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["create", "patch", "delete", "write_file", "remove_file"],
            "description": "操作类型: create（新建技能）/ patch（修改 SKILL.md）/ delete（删除技能）/ write_file（写支持文件）/ remove_file（删除支持文件）",
        },
        "name": {
            "type": "string",
            "description": "技能名称（同时也是目录名）",
        },
        "content": {
            "type": "string",
            "description": "create 时使用：完整的 SKILL.md 内容（含 frontmatter）",
        },
        "old_string": {
            "type": "string",
            "description": "patch 时使用：要替换的旧文本",
        },
        "new_string": {
            "type": "string",
            "description": "patch 时使用：替换后的新文本",
        },
        "replace_all": {
            "type": "boolean",
            "description": "patch 时使用：是否替换所有匹配（默认只替换第一个）",
        },
        "file_path": {
            "type": "string",
            "description": "write_file/remove_file 时使用：技能目录内的相对路径（如 references/api.md、scripts/tool.py）",
        },
        "file_content": {
            "type": "string",
            "description": "write_file 时使用：文件内容",
        },
    },
    "required": ["action", "name"],
}


# ---------------------------------------------------------------------------
# Register all built-in tools
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# memory_write — write to memory.md
# ---------------------------------------------------------------------------

MEMORY_WRITE_DEF = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["append", "update", "delete"],
            "description": "append: add new entry / update: replace existing / delete: remove",
        },
        "section": {
            "type": "string",
            "description": "Category, e.g. user_info, preference, tips, project_context",
        },
        "content": {
            "type": "string",
            "description": "Entry content (used for append/update)",
        },
        "key": {
            "type": "string",
            "description": "Lookup key (used for update/delete)",
        },
    },
    "required": ["action", "section"],
}


def memory_write(args: dict, ctx: dict) -> ToolResult:
    """Write/update/delete entries in memory.md"""
    action = args.get("action", "")
    section = args.get("section", "")
    content = args.get("content", "")
    key = args.get("key", "")

    inst_dir = os.getenv("INSTANCE_DIR", "")
    if not inst_dir:
        return ToolResult(ok=False, error="INSTANCE_DIR not set")

    path = os.path.join(inst_dir, "memory.md")
    if not os.path.isfile(path):
        try:
            os.makedirs(inst_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("# NanoGhost Memory\n\n")
        except Exception as e:
            return ToolResult(ok=False, error=f"Cannot create memory.md: {e}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return ToolResult(ok=False, error=f"Cannot read memory.md: {e}")

    if action == "append":
        header = f"## {section}"
        entry = content if content.startswith("- ") else f"- {content}"
        if header in text:
            text = text.replace(header, header + "\n" + entry, 1)
        else:
            text += f"\n## {section}\n{entry}\n"

    elif action == "update":
        if not key:
            return ToolResult(ok=False, error="key required for update")
        old_pattern = f"- {key}:"
        for line in text.split("\n"):
            if line.strip().startswith(old_pattern):
                new_line = f"- {key}: {content}" if content else f"- {key}"
                text = text.replace(line, new_line, 1)
                break
        else:
            return ToolResult(ok=False, error=f"Not found: {old_pattern}")

    elif action == "delete":
        if not key:
            return ToolResult(ok=False, error="key required for delete")
        text = "\n".join(
            l for l in text.split("\n")
            if not l.strip().startswith(f"- {key}:")
        )

    else:
        return ToolResult(ok=False, error=f"Unknown action: {action}")

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        return ToolResult(ok=False, error=f"Cannot write memory.md: {e}")

    return ToolResult(ok=True, data=f"memory.md {action} ok")


def _has_skills_dir() -> bool:
    return os.path.isdir(AGENTS_SKILLS_DIR)


def _has_npx() -> bool:
    return shutil.which("npx") is not None


def register_builtins(registry: Any) -> None:
    """Register all built-in tools into a ToolRegistry instance."""
    # ---- system (always available) ----
    registry.register("terminal", terminal,
                      description="在本地终端执行 shell 命令。执行代码、运行脚本、访问文件系统时使用。"
                      " 注意：联网搜索请使用 web-search 技能，不要用 terminal 手写爬虫。",
                      parameters=TERMINAL_DEF, category="system")
    registry.register("read", read_file,
                      description="读取本地文件内容。"
                                  "读取 SKILL.md 引用的 references/ 或 scripts/ 文件时使用。"
                                  "path 必须是绝对路径。",
                      parameters=READ_DEF, category="system")
    registry.register("ask_user", ask_user,
                      description="向用户提问并等待回答。当需要用户确认或选择时使用。",
                      parameters=ASK_USER_DEF, category="system")

    # ---- skill (需要 ~/.agents/skills/ 目录) ----
    has_skills = _has_skills_dir()
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
    # skill_install 额外需要 npx
    registry.register("skill_install", skill_install,
                      description="从生态安装一个技能包。"
                                  "安装后自动重新发现技能。使用前可先用 skills_list 查看可用技能。",
                      parameters=SKILL_INSTALL_DEF, category="skill",
                      check_fn=lambda: _has_skills_dir() and _has_npx())

    # ---- memory ----
    registry.register("memory_write", memory_write,
                      description="Write/update/delete entries in memory.md. "
                                  "Use this to remember user preferences, project info, tips, etc.",
                      parameters=MEMORY_WRITE_DEF, category="system")

    # ---- subagent (always available) ----
    registry.register("delegate_task", delegate_task,
                      description="将任务委派给子代理在隔离上下文中执行。"
                                  "子代理拥有独立的会话上下文，完成后返回结果。"
                                  "可用于并行探索、后台处理或隔离复杂子任务。",
                      parameters=DELEGATE_TASK_DEF, category="subagent")
