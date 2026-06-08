import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict

from agent_core.skill.discovery import AGENTS_SKILLS_DIR

from ..models import ToolResult

logger = logging.getLogger("agent_core")

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


def skills_list(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """List all available skills (name + description only)."""
    agent = ctx.get("agent")
    if agent is None:
        return ToolResult(ok=False, error="Agent 不可用")
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


def skill_install(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """Install a skill package from the ecosystem using npx skills add."""
    package = (args.get("package") or "").strip()
    if not package:
        return ToolResult(ok=False, error="缺少 package 参数（格式: owner/repo）")
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


def skill_manage(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """Manage skills: create, patch, delete, write/remove supporting files."""
    action = (args.get("action") or "").strip()
    name = (args.get("name") or "").strip()
    if not action or not name:
        return ToolResult(ok=False, error="缺少 action 或 name 参数")
    skill_dir = os.path.join(AGENTS_SKILLS_DIR, name)
    skill_md = os.path.join(skill_dir, "SKILL.md")
    agent = ctx.get("agent")
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
    if action == "delete":
        if not os.path.isdir(skill_dir):
            return ToolResult(ok=False, error=f"技能 [{name}] 不存在", signal="__continue__")
        shutil.rmtree(skill_dir)
        if agent:
            try:
                agent.discover_skills()
            except Exception:
                pass
        return ToolResult(ok=True, data=f"技能 [{name}] 已删除", signal="__continue__")
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


def _has_skills_dir() -> bool:
    return os.path.isdir(AGENTS_SKILLS_DIR)


def _has_npx() -> bool:
    return shutil.which("npx") is not None
