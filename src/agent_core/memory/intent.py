import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from agent_core.interfaces import DatabasePort, LLMPort

logger = logging.getLogger("agent_core")


def extract_memory_md_entries(user_message: str, reply: str, steps: list) -> list[dict]:
    """从对话中提取值得记入 memory.md 的信息。纯字符串判断，不需要 LLM。"""
    entries = []

    # H1: 用户自称
    for prefix in ["叫我", "我是", "叫我了"]:
        if prefix in user_message:
            idx = user_message.find(prefix) + len(prefix)
            name = user_message[idx:].split("。")[0].split("，")[0].split(" ")[0].strip()
            if name and len(name) <= 10:
                entries.append({"section": "user_info", "content": f"- Name: {name}"})
                break

    # H2: 用户偏好
    for kw in ["喜欢", "不要", "倾向", "偏好"]:
        if kw in user_message:
            idx = user_message.find(kw)
            text = user_message[idx:].split("。")[0].split("，")[0].strip()
            if 3 < len(text) < 60:
                entries.append({"section": "preference", "content": f"- {text}"})
                break

    # H3: 回复中的建议
    for kw in ["建议", "注意", "推荐", "以后"]:
        if kw in reply:
            idx = reply.find(kw)
            sentence = reply[idx:].split("。")[0].split("!")[0].strip()
            if 5 < len(sentence) < 100:
                entries.append({"section": "tips", "content": f"- {sentence}"})
                break

    # H4: 路径提取（从 terminal 输出中）
    path_cmds = {"dir", "pwd", "where", "ls", "cd", "find"}
    for s in (steps or []):
        if s.get("method") != "EXEC":
            continue
        cmd = (s.get("path") or "").strip().split()[0] if s.get("path") else ""
        if cmd not in path_cmds:
            continue
        for line in (s.get("result_preview") or "").split("\n"):
            line = line.strip()
            if re.match(r"^[A-Z]:\\\\", line):
                entries.append({"section": "project_context", "content": f"- Path: {line}"})
                break

    return entries


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

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

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

    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


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
