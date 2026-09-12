import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent_core.interfaces import DatabasePort, LLMPort

from agent_core.interfaces import DatabasePort, LLMPort

logger = logging.getLogger("agent_core")



def _lock_file(f):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)


def _unlock_file(f):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def append_to_memory_md(db, namespace: str, entries: list[dict]):
    """将条目写入 memory.md 文件"""
    inst_dir = os.getenv("INSTANCE_DIR", "")
    if not inst_dir:
        return

    path = os.path.join(inst_dir, "memory.md")
    MAX_LINES = 200

    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("# NanoGhost Memory\n\n")

    with open(path, "r+", encoding="utf-8") as f:
        try:
            _lock_file(f)
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    text = f.read()
                    break
                except Exception:
                    time.sleep(0.05)

            for entry in entries:
                section, line = entry["section"], entry["content"]
                if line in text:
                    continue
                header = f"## {section}"
                if header in text:
                    text = text.replace(header, header + "\n" + line, 1)
                else:
                    text += f"\n## {section}\n{line}\n"

            lines = text.split("\n")
            if len(lines) > MAX_LINES:
                text = "\n".join(lines[:MAX_LINES]) + "\n\n<!-- truncated -->"

            f.seek(0)
            f.truncate()
            f.write(text)
        finally:
            _unlock_file(f)


def summarize_intent(
    db: DatabasePort, session_id: Optional[str], llm: Optional[LLMPort], current_message: str,
) -> str:
    """从会话历史中提取用户真实意图。多轮对话用 LLM 总结，失败则不记录。"""
    current = (current_message or "").strip()
    if not current:
        return ""

    try:
        history = db.get_agent_messages(session_id) if session_id else []
    except Exception:
        history = []

    user_msgs = []
    seen = set()
    for msg in history:
        if isinstance(msg, dict) and msg.get("role") == "user" and msg.get("type") == "text":
            txt = (msg.get("content") or "").strip()
            if txt and txt not in seen:
                seen.add(txt)
                user_msgs.append(txt)

    prev_msgs = [m for m in user_msgs if m != current]
    if not prev_msgs:
        # 单条消息，直接用
        return current

    # 多轮对话，用 LLM 总结
    if not llm:
        return ""  # 没有 LLM 就不记录
    context_lines = "\n".join(f"- {m[:200]}" for m in prev_msgs[-3:])
    prompt = (
        "以下是一个用户与AI助手的对话历史中，用户说过的消息（按时间顺序）：\n"
        f"{context_lines}\n\n"
        f"用户最后说：{current}\n\n"
        "请用一句话总结用户在整个对话中的真实意图/任务需求（20字以内）："
    )
    try:
        resp = llm.chat([{"role": "user", "content": [{"type": "text", "text": prompt}]}])
        if resp and resp.content:
            summary = resp.content.strip().strip("\u201c\u201d\u3002")
            if summary:
                logger.info(f"[AgentMemory] 意图总结: {summary}")
                return summary
    except Exception:
        pass
    return ""  # LLM 失败，不记录


def summarize_to_memory_md(
    llm: LLMPort,
    user_message: str,
    reply: str,
    session_id: Optional[str],
    db: DatabasePort,
    round_number: int,
) -> list[dict]:
    """每 N 轮用 LLM 判断是否有值得记入 memory.md 的信息。"""
    if round_number % 3 != 0:
        return []
    try:
        history = db.get_agent_messages(session_id) if session_id else []
    except Exception:
        history = []

    # 取最近 3 轮对话
    recent = []
    for msg in (history or []):
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant") and msg.get("type") == "text":
            txt = (msg.get("content") or "").strip()
            if txt:
                recent.append(f"{msg['role']}: {txt[:200]}")
    recent = recent[-6:]  # 3 user + 3 assistant

    if not recent:
        return []

    context = "\n".join(recent)
    prompt = (
        "以下是最近几轮对话：\n"
        f"{context}\n\n"
        "请判断是否有值得记住的信息，例如：用户个人信息、偏好、项目上下文、重要约定。\n"
        "如果有，输出 JSON 数组，每个元素包含 section 和 content，例如：\n"
        '[{"section": "user_info", "content": "- 用户叫张三"}, {"section": "preference", "content": "- 喜欢简洁回复"}]\n'
        "如果没有值得记的，输出 []\n"
        "只输出 JSON，不要其他文字："
    )
    try:
        resp = llm.chat([{"role": "user", "content": [{"type": "text", "text": prompt}]}])
        if resp and resp.content:
            import json
            entries = json.loads(resp.content.strip())
            if isinstance(entries, list):
                for e in entries:
                    if not isinstance(e, dict) or "section" not in e or "content" not in e:
                        return []
                logger.info(f"[AgentMemory] LLM extracted {len(entries)} memory.md entries at round {round_number}")
                return entries
    except Exception:
        pass
    return []
