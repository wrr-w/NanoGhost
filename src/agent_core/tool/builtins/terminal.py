import logging
import os
import subprocess
from typing import Any, Dict, Optional, Tuple

from ..models import ToolResult

logger = logging.getLogger("agent_core")


def _execute_shell_command(
    command: str,
    step_num: int,
    timeout: int = 30,
    workdir: Optional[str] = None,
) -> Tuple[Dict, bool, Optional[str]]:
    """执行本地 shell 命令。"""
    logger.info(f"[ShellExec] step {step_num}: {command[:200]}")
    proc = None
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir or os.getcwd(),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        ok = proc.returncode == 0
        stdout = (stdout or b"").decode("utf-8", errors="replace")
        stderr = (stderr or b"").decode("utf-8", errors="replace")
        preview = ""
        if stdout:
            preview = stdout[:4000]
            if len(stdout) > 4000:
                preview += "\n…（输出已截断）"
        if stderr:
            if preview:
                preview += "\n--- stderr ---\n"
            preview += stderr[:2000]
            if len(stderr) > 2000:
                preview += "\n…（stderr 已截断）"
        step_out = {
            "step": step_num, "method": "EXEC", "path": command,
            "ok": ok, "exit_code": proc.returncode, "result_preview": preview,
        }
        return step_out, ok, None if ok else f"exit code {proc.returncode}"
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                proc.kill()
                if os.name == "nt":
                    import subprocess as _sp
                    _sp.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=5)
                proc.wait(timeout=5)
            except Exception:
                pass
        return {"step": step_num, "method": "EXEC", "path": command, "ok": False, "error": f"命令超时（{timeout}秒）", "exit_code": -1}, False, f"命令超时（{timeout}秒）"
    except FileNotFoundError as e:
        return {"step": step_num, "method": "EXEC", "path": command, "ok": False, "error": f"命令未找到: {e}"}, False, str(e)
    except Exception as e:
        return {"step": step_num, "method": "EXEC", "path": command, "ok": False, "error": str(e)}, False, str(e)


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
