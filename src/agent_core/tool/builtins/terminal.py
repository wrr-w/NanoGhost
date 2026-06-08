import logging
from typing import Any, Dict

from agent_core.engine.executor import _execute_shell_command

from ..models import ToolResult

logger = logging.getLogger("agent_core")


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
