import json
import logging
import os
import re
import subprocess
import sys
from typing import Any, Dict, Optional, Tuple

from agent_core.interfaces import HttpPort

logger = logging.getLogger("agent_core")

_STEP_RESULT_JSON_MAX_LEN = 8000

_STATE_RELATED_ERRORS = {
    "console_not_started",
    "not_running",
    "task_not_running",
}


def _fetch_system_status(http: HttpPort, base_url: str) -> Dict[str, Any]:
    """查询系统状态（用于失败时的钩子）。"""
    try:
        status_code, data = http.request("GET", f"{base_url}/api/agent/status/summary", timeout=10)
        if status_code == 200 and data.get("ok"):
            return {
                "running": data.get("running", False),
                "running_tasks": data.get("running_tasks", 0),
                "db_ok": data.get("db_ok", True),
            }
    except Exception:
        pass
    return {"running": False, "running_tasks": 0, "db_ok": False, "fetch_failed": True}


def _is_state_related_error(error: str) -> bool:
    if not error:
        return False
    error_lower = error.lower().strip()
    return any(e in error_lower for e in _STATE_RELATED_ERRORS)


def _get_nested(obj: Any, path: str) -> Any:
    if obj is None:
        return None
    keys = path.split(".")
    for i, key in enumerate(keys):
        if key == "length" and i == len(keys) - 1 and isinstance(obj, list):
            return len(obj)
        if isinstance(obj, dict) and key in obj:
            obj = obj[key]
        elif isinstance(obj, list) and key.isdigit():
            idx = int(key)
            if 0 <= idx < len(obj):
                obj = obj[idx]
            else:
                return None
        else:
            return None
    return obj


def _hint_placeholders_for_next_step(step_results: Dict[int, Dict]) -> str:
    if not step_results:
        return ""
    success_steps = [
        num for num, data in sorted(step_results.items())
        if isinstance(data, dict) and data.get("ok") is not False
    ]
    if not success_steps:
        return ""
    return (
        f"\n【引用数据】已执行的步骤: {', '.join(f'步骤{n}' for n in success_steps)}，"
        f"可用 {{{{stepN.xxx.yyy}}}} 引用返回字段。\n"
    )


def _resolve_placeholders(value: Any, step_results: Dict[int, Dict], user_message: str) -> Any:
    if isinstance(value, str):
        text = value
        text = text.replace("{{user_original_intent}}", user_message)
        for match in re.finditer(r"\{\{step(\d+)\.([^}]+)\}\}", text):
            step_num = int(match.group(1))
            key_path = match.group(2).strip()
            res = step_results.get(step_num)
            if res is None:
                warning = f"⚠️[步骤{step_num}未执行]"
            else:
                val = _get_nested(res, key_path)
                if val is None:
                    warning = f"[step{step_num}.{key_path}不存在]"
                else:
                    warning = None
            if warning:
                text = text.replace(match.group(0), warning)
            else:
                text = text.replace(match.group(0), str(val))
        return text
    if isinstance(value, dict):
        return {k: _resolve_placeholders(v, step_results, user_message) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(v, step_results, user_message) for v in value]
    return value


def _execute_one_step(
    item: Dict,
    step_num: int,
    step_results: Dict[int, Dict],
    user_message: str,
    base_url: str,
    http: HttpPort,
) -> Tuple[Dict, bool, Optional[str]]:
    """执行单步，返回 (step_out_dict, ok, error_msg)。"""
    method = (item.get("method") or "GET").upper()
    path = _resolve_placeholders(item.get("path") or "", step_results, user_message)
    body_raw = item.get("body")
    body = _resolve_placeholders(body_raw, step_results, user_message) if body_raw is not None else None

    placeholder_error = None
    if "⚠️[" in path:
        placeholder_error = f"占位符解析失败: {path}"
    elif body and isinstance(body, dict):
        for k, v in body.items():
            if isinstance(v, str) and "⚠️[" in v:
                placeholder_error = f"占位符解析失败: body.{k} = {v}"
                break

    if placeholder_error:
        return {
            "step": step_num,
            "method": method,
            "path": path,
            "ok": False,
            "error": placeholder_error,
        }, False, placeholder_error

    url = (base_url.rstrip("/") + path) if path.startswith("/") else (base_url + path)
    try:
        status_code, data = http.request(method, url, body=body if body is not None else None, timeout=120)
        ok = status_code < 400 and data.get("ok", True)
        step_out = {
            "step": step_num,
            "method": method,
            "path": path,
            "ok": ok,
            "status_code": status_code,
            "error": data.get("error") if not data.get("ok") else None,
        }
        if ok:
            step_results[step_num] = data
            logger.info(f"[_execute_one_step] data for step {step_num}: {data}")
            if path == "/api/get_image_by_id":
                if data.get("images"):
                    step_out["images"] = data["images"]
                    step_out["result_preview"] = f"批量获取图片（{len(data['images'])}张）"
                else:
                    step_out["result_preview"] = "图片接口响应（无数据）"
            else:
                raw_json = json.dumps(data, ensure_ascii=False, indent=2)
                if len(raw_json) > _STEP_RESULT_JSON_MAX_LEN:
                    raw_json = raw_json[:_STEP_RESULT_JSON_MAX_LEN] + "\n…（已截断）"
                step_out["result_preview"] = raw_json
            return step_out, True, None
        return step_out, False, data.get("error") or f"HTTP {status_code}"
    except Exception as e:
        return {
            "step": step_num,
            "method": method,
            "path": path,
            "ok": False,
            "error": str(e),
        }, False, str(e)


def _execute_shell_command(
    command: str,
    step_num: int,
    timeout: int = 120,
    workdir: Optional[str] = None,
) -> Tuple[Dict, bool, Optional[str]]:
    """执行本地 shell 命令。

    Args:
        command: 要执行的命令。
        step_num: 当前步骤编号。
        timeout: 超时秒数。
        workdir: 工作目录（默认当前目录）。

    Returns:
        (step_out_dict, ok, error_msg)
    """
    logger.info(f"[ShellExec] step {step_num}: {command[:200]}")

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir or os.getcwd(),
        )
        ok = result.returncode == 0
        stdout = result.stdout or ""
        stderr = result.stderr or ""

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
            "step": step_num,
            "method": "EXEC",
            "path": command,
            "ok": ok,
            "exit_code": result.returncode,
            "result_preview": preview,
        }
        return step_out, ok, None if ok else f"exit code {result.returncode}"

    except subprocess.TimeoutExpired:
        return {
            "step": step_num, "method": "EXEC", "path": command,
            "ok": False, "error": f"命令超时（{timeout}秒）",
            "exit_code": -1,
        }, False, f"命令超时（{timeout}秒）"
    except FileNotFoundError as e:
        return {
            "step": step_num, "method": "EXEC", "path": command,
            "ok": False, "error": f"命令未找到: {e}",
        }, False, str(e)
    except Exception as e:
        return {
            "step": step_num, "method": "EXEC", "path": command,
            "ok": False, "error": str(e),
        }, False, str(e)
