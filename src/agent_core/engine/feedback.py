import json
import logging
from typing import Any, Dict, Optional, Tuple

from agent_core.interfaces import HttpPort, DatabasePort
from agent_core.engine.executor import (
    _hint_placeholders_for_next_step,
    _fetch_system_status,
    _is_state_related_error,
)
from agent_core.memory.graph import suggest_next_nodes

logger = logging.getLogger("agent_core")


def _format_step_result(step_out: Dict, step_results: Dict[int, Dict]) -> str:
    step_num = step_out.get("step", 0)
    method = step_out.get("method", "GET")
    path = step_out.get("path", "")
    ok = step_out.get("ok", False)
    error = step_out.get("error", "")

    lines = [f"步骤 {step_num}: {method} {path} — {'成功' if ok else '失败'}"]
    if error:
        lines.append(f"错误: {error}")

    preview = step_out.get("result_preview")
    if preview:
        lines.append(f"输出: {preview[:2000]}")
    elif ok and step_num in step_results:
        if path == "/api/get_image_by_id":
            img_data = step_results[step_num]
            img_count = len(img_data.get("images", {})) if isinstance(img_data, dict) else 0
            lines.append(f"返回: 成功获取 {img_count} 张图片（已通过 image_url 注入上下文）")
        else:
            raw = json.dumps(step_results[step_num], ensure_ascii=False)
            lines.append(f"返回: {raw}")
    return "\n".join(lines)


def _build_step_feedback(
    step_out: Dict,
    all_step_results: Dict[int, Dict],
    action: Dict,
    ok: bool,
    error: str,
    base_url: str,
    http: HttpPort,
    db: DatabasePort,
    namespace: Optional[str] = None,
) -> Tuple[str, str]:
    """构建步骤反馈，返回 (obs, status_message)。"""
    obs = _format_step_result(step_out, all_step_results)

    if ok:
        all_step_results[step_out["step"]] = all_step_results.get(step_out["step"], {})
        obs += _hint_placeholders_for_next_step(all_step_results)

        suggestions = suggest_next_nodes(action, top_k=3, db=db, namespace=namespace)
        if suggestions:
            sug_lines = []
            for s in suggestions:
                rel = s.get("relation_type", "FOLLOWS")
                rel_label = "数据依赖" if rel == "DEPENDS_ON" else "时序"
                sug_lines.append(
                    f"{s['method']} {s['path']} [{rel_label}] "
                    f"(认可率{s['approved_ratio']})"
                )
            obs += f"\n\n【推荐下一步】\n" + "\n".join(f"- {l}" for l in sug_lines)
        obs += "\n\n请决定：执行推荐动作、或输出其他动作、或 done:true 结束。"
        return obs, "步骤成功，已注入推荐…"

    if _is_state_related_error(error):
        status = _fetch_system_status(http, base_url)
        obs += f"\n\n【系统状态】\n{json.dumps(status, ensure_ascii=False)}"
        obs += "\n\n请根据状态决定下一步。"
        return obs, "步骤失败，已注入系统状态…"

    obs += _hint_placeholders_for_next_step(all_step_results)
    obs += "\n\n请决定：重试、换路径、或结束。"
    return obs, "步骤失败，等待重试决策…"
