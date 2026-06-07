import json
import time
import logging
from typing import Any, Dict, List, Optional

from agent_core.interfaces import DatabasePort, LLMPort
from agent_core.memory.cards import retrieve_similar_flows

logger = logging.getLogger("agent_core")


def _estimate_message_tokens(msg: Dict[str, Any]) -> int:
    """粗略估算单条 message 的 token 数。

    混合文本按字符数/3估算；图片消息按固定值估算，避免 base64 长度误导。
    reasoning_content 也计入 token。
    """
    total = 0

    # reasoning_content
    rc = msg.get("reasoning_content")
    if isinstance(rc, str):
        total += max(1, len(rc) // 3)

    content = msg.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type", "")
                if ptype == "image_url":
                    total += 1000  # 图片固定估算
                else:
                    text = part.get("text") or ""
                    total += max(1, len(text) // 3)
            else:
                total += max(1, len(str(part)) // 3)
    elif isinstance(content, str):
        total += max(1, len(content) // 3)
    else:
        total += max(1, len(str(content)) // 3)

    # tool_calls 文本
    tcs = msg.get("tool_calls")
    if isinstance(tcs, list):
        total += max(1, len(json.dumps(tcs, ensure_ascii=False)) // 3)

    return max(1, total)


def _truncate_history_by_tokens(history_msgs: List[Dict[str, Any]], max_tokens: int) -> List[Dict[str, Any]]:
    """如果历史消息总 token 超过上限，从最早的消息开始丢弃。"""
    if not history_msgs:
        return history_msgs

    total = sum(_estimate_message_tokens(m) for m in history_msgs)
    if total <= max_tokens:
        return history_msgs

    # 从旧到新丢弃，直到满足限制
    while history_msgs and total > max_tokens:
        removed = history_msgs.pop(0)
        total -= _estimate_message_tokens(removed)

    logger.info(
        f"[AgentHistory] truncated by tokens: kept={len(history_msgs)}, "
        f"estimated_tokens={total}, limit={max_tokens}"
    )
    return history_msgs


def build_agent_messages_with_history(
    session_id: Optional[str],
    sys_prompt: str,
    user_message: str,
    db: DatabasePort,
    user_images: Optional[List[str]] = None,
    image_urls: Optional[List[str]] = None,
    llm: Optional[LLMPort] = None,
    namespace: Optional[str] = None,
    history_max_messages: int = 200,
    history_max_tokens: int = 200_000,
    root_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """拼装带会话历史 + 记忆召回的 messages。

    Args:
        namespace: 多实例隔离标签,传递给记忆检索。
        history_max_messages: 保留的最大历史消息条数。
        history_max_tokens: 历史消息的估算 token 上限，超出从旧消息截断。
        root_id: 话题根消息 ID。非空时只加载同 root_id 的历史消息。
    """
    out: List[Dict[str, Any]] = [{"role": "system", "content": [{"type": "text", "text": sys_prompt}]}]

    logger.debug(
        f"[Agent] build_messages: user_images={len(user_images) if user_images else 0}, "
        f"user_message_length={len(user_message)}"
    )

    _t_retrieve = time.time()
    try:
        similar_flows = retrieve_similar_flows(
            user_message, top_k=2, increment_trigger=False,
            db=db, llm=llm, namespace=namespace,
        ) or []
    except Exception as e:
        logger.error(f"[AgentMemory] retrieve_similar_flows error: {e}")
        similar_flows = []

    logger.info(f"[AgentMemory] retrieve_similar_flows 耗时={time.time()-_t_retrieve:.1f}s, found={len(similar_flows)}")

    if similar_flows:
        lines: List[str] = []
        for idx, m in enumerate(similar_flows, start=1):
            intent = (m.get("intent_summary") or "").strip()
            steps = m.get("steps") or []
            pitfalls = m.get("pitfalls") or []
            experiences = m.get("experience_notes") or []

            step_text = " -> ".join(
                f"{s.get('method','')} {s.get('path','')}" for s in (steps[:5] if isinstance(steps, list) else [])
            )
            part = f"{idx}. {intent or '（无摘要）'}"
            if step_text:
                part += f"\n   步骤: {step_text}"
            if pitfalls:
                part += "\n   踩坑: " + "; ".join(pitfalls[:3])
            if experiences:
                part += "\n   经验: " + "; ".join(experiences[:2])
            lines.append(part)
        mem_text = "【历史相似流程】\n" + "\n".join(lines) + "\n\n可参考这些流程。注意踩坑提醒。"
        out.append({"role": "system", "content": [{"type": "text", "text": mem_text}]})

    history_msgs: List[Dict[str, Any]] = []
    if session_id:
        history = db.get_agent_messages(session_id, root_id=root_id)
        if history:
            # Phase 1: 按条数截断（保留最新的）
            if len(history) > history_max_messages:
                history = history[-history_max_messages:]
                logger.info(
                    f"[AgentHistory] truncated by count: kept={len(history)}, limit={history_max_messages}"
                )

            history_image_ids = []
            for m in history:
                if m.get("type") == "image":
                    img_id = m.get("content", "").strip()
                    history_image_ids.append(img_id)

            images_cache = {}
            if history_image_ids:
                images_data = db.get_agent_images_batch(history_image_ids)
                images_cache = {img["id"]: img["base64"] for img in images_data}

            for m in history:
                role = m.get("role") or "user"
                type = m.get("type") or "text"
                content = (m.get("content") or "").strip()
                if role == "user":
                    if type == "image":
                        img_base64 = images_cache.get(content, content)
                        history_msgs.append({"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": img_base64}},
                            {"type": "text", "text": f"_image_reference: {content}"},
                        ]})
                    else:
                        history_msgs.append({"role": "user", "content": [{"type": "text", "text": content}]})
                else:
                    steps_json = m.get("steps_json")
                    reasoning_content = m.get("reasoning_content")
                    assistant_content = [{"type": "text", "text": content}]
                    assistant_msg = {"role": "assistant", "content": assistant_content}
                    if reasoning_content:
                        assistant_msg["reasoning_content"] = reasoning_content
                    if steps_json:
                        try:
                            steps = json.loads(steps_json)
                            if isinstance(steps, list) and steps:
                                lines = []
                                for s in steps:
                                    line = f"步骤{s.get('step')} {s.get('method', '')} {s.get('path', '')}"
                                    prev = s.get("result_preview") or ""
                                    if prev:
                                        line += ": " + (prev[:300] + "…" if len(prev) > 300 else prev)
                                    lines.append(line)
                                summary = "\n\n【执行摘要】\n" + "\n".join(lines)
                                assistant_content.append({"type": "text", "text": summary})
                        except Exception:
                            pass
                    history_msgs.append(assistant_msg)

            # Phase 2: 按 token 截断（从旧消息丢弃）
            history_msgs = _truncate_history_by_tokens(history_msgs, history_max_tokens)

    out.extend(history_msgs)

    current_user_content = []
    if user_images:
        for i, img in enumerate(user_images):
            current_user_content.append({"type": "image_url", "image_url": {"url": img}})
            if image_urls and i < len(image_urls):
                current_user_content.append({"type": "text", "text": f"_image_reference: {image_urls[i]}"})

    if user_message:
        current_user_content.append({
            "type": "text",
            "text": user_message,
        })

    if current_user_content:
        out.append({"role": "user", "content": current_user_content})

    return out
