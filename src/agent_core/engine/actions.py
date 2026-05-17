import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from agent_core.memory.cards import retrieve_similar_flows

logger = logging.getLogger("agent_core")


def _format_detail_for_llm(detail: Dict) -> str:
    """将 api_spec detail 格式化为 LLM 可读文本。"""
    parts = [f"## {detail.get('slug', '')}"]
    parts.append(f"{detail.get('method', 'GET').upper()} {detail.get('path', '')}")
    summary = (detail.get("description") or detail.get("summary") or "").strip()
    if summary:
        parts.append(f"\n{summary}")
    params = detail.get("parameters") or []
    if params:
        parts.append("\n参数:")
        for p in params:
            required = "必填" if p.get("required", False) else "可选"
            desc = (p.get("description") or "").strip()
            parts.append(f"  - {p.get('name')} ({p.get('in', 'query')}, {required}): {desc or p.get('type', 'str')}")
    request_body = detail.get("request_body")
    if isinstance(request_body, dict):
        props = request_body.get("properties") or {}
        if props:
            parts.append("\n请求体:")
            for k, v in props.items():
                desc = (v.get("description") or "").strip()
                parts.append(f"  - {k} ({v.get('type', 'str')}): {desc or '-'}")
    responses = detail.get("responses") or {}
    resp_200 = responses.get("200") or responses.get("default") or {}
    resp_desc = resp_200.get("description") or ""
    if resp_desc:
        parts.append(f"\n返回: {resp_desc}")
    return "\n".join(parts)


def _get_detail_from_spec(api_spec: Dict, slug: str) -> Optional[Dict]:
    """从 api_spec 中按 slug 查找接口详情。"""
    details = api_spec.get("details") or {}
    return details.get(slug)


def _handle_need_slugs(
    parsed: Dict, full_content: str, api_spec: Dict,
) -> Optional[List[Dict]]:
    """处理 need_slugs，返回需要添加的消息列表，或 None。"""
    need_slugs = parsed.get("need_slugs")
    if not isinstance(need_slugs, list) or not need_slugs:
        return None

    parts = []
    for slug in need_slugs:
        if isinstance(slug, str):
            d = _get_detail_from_spec(api_spec, slug.strip())
            if d:
                parts.append(_format_detail_for_llm(d))
    if parts:
        follow_up = "接口详情：\n\n" + "\n---\n".join(parts) + "\n\n请继续。"
        return [
            {"role": "assistant", "content": full_content},
            {"role": "user", "content": follow_up},
        ]
    return None


def _handle_ask_user(parsed: Dict, session_id: str) -> Tuple[str, Dict]:
    ask_data = parsed["ask_user"]

    content_arr = ask_data.get("content")
    question = "请确认"

    if content_arr and isinstance(content_arr, list):
        for item in content_arr:
            if isinstance(item, str):
                question = item
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    question = item.get("content", "请确认")
            elif isinstance(item, list) and len(item) >= 2:
                item_type, item_content = item[0], item[1]
                if item_type == "text":
                    question = item_content
    else:
        question = ask_data.get("question") or "请确认"

    options = ask_data.get("options") or []
    processed_options = []
    for opt in options:
        if isinstance(opt, dict):
            opt_type = opt.get("type", "text")
            opt_content = opt.get("content", "")
            processed_options.append({"type": opt_type, "content": opt_content})
        elif isinstance(opt, str):
            processed_options.append({"type": "text", "content": opt})
        elif isinstance(opt, list) and len(opt) >= 2:
            processed_options.append({"type": opt[0], "content": opt[1]})

    payload = {"question": question, "options": processed_options, "session_id": session_id}
    if content_arr:
        payload["content_arr"] = content_arr
    return ("ask_user", payload)


def _handle_memory_tool(
    action: Dict, user_message: str, full_content: str,
    db=None, llm=None, namespace=None,
) -> Optional[List[Dict]]:
    """处理 MEMORY 工具，返回需要添加的消息列表，或 None。"""
    method = (action.get("method") or "").upper()
    path = (action.get("path") or "").strip()
    if method != "MEMORY" or path != "/agent/memory/retrieve":
        return None

    body = action.get("body") or {}
    intent_summary = (body.get("intent_summary") or "").strip() or user_message
    try:
        similar = retrieve_similar_flows(
            intent_summary,
            top_k=int(body.get("top_k") or 3),
            increment_trigger=True,
            db=db, llm=llm, namespace=namespace,
        )
    except Exception:
        similar = []
    obs = "【记忆检索结果】\n" + (
        "\n".join(f"{i}. {f.get('intent_summary', '')[:50]}" for i, f in enumerate(similar, 1))
        if similar else "未找到相似流程"
    )
    obs += "\n\n请继续。"
    return [
        {"role": "assistant", "content": full_content},
        {"role": "user", "content": obs},
    ]
