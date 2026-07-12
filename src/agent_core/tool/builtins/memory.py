import logging
import os
from datetime import date
from typing import Any

from agent_core.memory.cards import get_card_detail, list_card_index
from agent_core.memory.classifier import classify, level_name
from agent_core.memory.files import (
    daily_memory_path,
    ensure_memory_layout,
    long_term_memory_path,
)

from ..models import ToolResult

logger = logging.getLogger("agent_core")


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
        "target": {
            "type": "string",
            "enum": ["long_term", "daily"],
            "description": "Write to memory.md or today's daily memory file",
            "default": "long_term",
        },
    },
    "required": ["action", "section"],
}


def _resolve_memory_target_path(instance_dir: str, target: str) -> str:
    ensure_memory_layout(instance_dir)
    if target == "daily":
        return str(daily_memory_path(instance_dir, date.today().isoformat()))
    if target == "long_term":
        return str(long_term_memory_path(instance_dir))
    raise ValueError(f"Unknown target: {target}")


def memory_write(args: dict, ctx: dict) -> ToolResult:
    """Write/update/delete entries in memory.md"""
    action = args.get("action", "")
    section = args.get("section", "")
    content = args.get("content", "")
    key = args.get("key", "")
    target = args.get("target", "long_term")
    inst_dir = os.getenv("INSTANCE_DIR", "")
    if not inst_dir:
        return ToolResult(ok=False, error="INSTANCE_DIR not set")
    try:
        path = _resolve_memory_target_path(inst_dir, target)
    except ValueError as e:
        return ToolResult(ok=False, error=str(e))
    if not os.path.isfile(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("# NanoGhost Memory\n\n")
        except Exception as e:
            return ToolResult(ok=False, error=f"Cannot create memory file: {e}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return ToolResult(ok=False, error=f"Cannot read memory file: {e}")
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
        return ToolResult(ok=False, error=f"Cannot write memory file: {e}")
    return ToolResult(ok=True, data=f"{os.path.basename(path)} {action} ok")


MEMORY_EXPLORE_DEF = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["node", "drill"],
            "description": "node: view outgoing edges at L1+L2 level / drill: expand to L3",
        },
        "method": {
            "type": "string",
            "description": "Current step method (for action=node)",
        },
        "path": {
            "type": "string",
            "description": "Current step path (for action=node)",
        },
        "from_code": {
            "type": "integer",
            "description": "From code from node result (for action=drill)",
        },
        "to_code": {
            "type": "integer",
            "description": "To code from node result (for action=drill)",
        },
        "level": {
            "type": "integer",
            "description": "Detail level: 3=resource, 4=detail (for action=drill)",
            "default": 3,
        },
    },
    "required": ["action"],
}


def memory_explore(args: dict, ctx: dict) -> ToolResult:
    """Query the multi-layer operation graph."""
    action = args.get("action", "")
    db = ctx.get("db")
    namespace = ctx.get("namespace")
    if not db:
        return ToolResult(ok=False, error="Database not available")
    try:
        if action == "node":
            method = args.get("method", "")
            path_str = args.get("path", "")
            if not method or not path_str:
                return ToolResult(ok=False, error="method and path required for node action")
            code = classify(method, path_str)
            l1_edges = db.load_ml_edges(level=1, from_code=code.l1, namespace=namespace)
            l2_edges = db.load_ml_edges(level=2, from_code=code.level_code(2), namespace=namespace)
            result = {"L1": [], "L2": [], "from_code_l1": code.l1, "from_code_l2": code.level_code(2)}
            for e in l1_edges[:10]:
                result["L1"].append({"to": level_name(e["to_code"], 1), "count": e["total_count"]})
            for e in l2_edges[:10]:
                result["L2"].append({"to": level_name(e["to_code"], 2), "count": e["total_count"]})
            return ToolResult(ok=True, data=result)
        elif action == "drill":
            from_code = args.get("from_code", 0)
            to_code = args.get("to_code", 0)
            level = args.get("level", 3)
            if not from_code or not to_code:
                return ToolResult(ok=False, error="from_code and to_code required for drill")
            edges = db.load_ml_edges(level=level, from_code=from_code, namespace=namespace)
            filtered = [e for e in edges if e["to_code"] == to_code or (e["to_code"] >> 16) == (to_code >> 16)]
            if not filtered:
                filtered = edges[:10]
            result = {f"L{level}": []}
            for e in filtered[:10]:
                result[f"L{level}"].append({"to": level_name(e["to_code"], level), "count": e["total_count"]})
            return ToolResult(ok=True, data=result)
        else:
            return ToolResult(ok=False, error=f"Unknown action: {action}")
    except Exception as e:
        logger.error(f"[memory_explore] error: {e}")
        return ToolResult(ok=False, error=str(e))


MEMORY_READ_DEF = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["index", "section", "detail"],
            "description": "index: list sections / section: read one section / detail: search within section",
        },
        "section": {
            "type": "string",
            "description": "Section name (for action=section or detail)",
        },
        "keyword": {
            "type": "string",
            "description": "Keyword to filter (for action=detail)",
        },
        "target": {
            "type": "string",
            "enum": ["long_term", "daily"],
            "description": "Read from memory.md or today's daily memory file",
            "default": "long_term",
        },
    },
    "required": ["action"],
}


def memory_read(args: dict, ctx: dict) -> ToolResult:
    """Read memory.md with layered disclosure."""
    action = args.get("action", "")
    target = args.get("target", "long_term")
    inst_dir = os.getenv("INSTANCE_DIR", "")
    if not inst_dir:
        return ToolResult(ok=False, error="INSTANCE_DIR not set")
    try:
        path_md = _resolve_memory_target_path(inst_dir, target)
    except ValueError as e:
        return ToolResult(ok=False, error=str(e))
    if not os.path.isfile(path_md):
        return ToolResult(ok=True, data={"message": f"empty (no {os.path.basename(path_md)} yet)"})
    try:
        with open(path_md, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        return ToolResult(ok=False, error=f"Cannot read memory file: {e}")
    if action == "index":
        sections = []
        current = None
        count = 0
        for line in text.split("\n"):
            if line.startswith("## "):
                if current:
                    sections.append({"section": current, "lines": count})
                current = line.strip("# ").strip()
                count = 0
            elif current:
                if line.strip():
                    count += 1
        if current:
            sections.append({"section": current, "lines": count})
        return ToolResult(ok=True, data={"sections": sections})
    elif action == "section":
        section_name = args.get("section", "")
        if not section_name:
            return ToolResult(ok=False, error="section required")
        header = f"## {section_name}"
        if header not in text:
            return ToolResult(ok=True, data={"section": section_name, "content": []})
        parts = text.split(header)
        content_part = parts[1].split("\n## ")[0]
        lines = [l.strip() for l in content_part.split("\n") if l.strip()]
        return ToolResult(ok=True, data={"section": section_name, "content": lines})
    elif action == "detail":
        section_name = args.get("section", "")
        keyword = args.get("keyword", "")
        if not section_name or not keyword:
            return ToolResult(ok=False, error="section and keyword required")
        header = f"## {section_name}"
        if header not in text:
            return ToolResult(ok=True, data={"section": section_name, "matches": []})
        parts = text.split(header)
        content_part = parts[1].split("\n## ")[0]
        matches = [l.strip() for l in content_part.split("\n") if keyword.lower() in l.lower()]
        return ToolResult(ok=True, data={"section": section_name, "keyword": keyword, "matches": matches})
    else:
        return ToolResult(ok=False, error=f"Unknown action: {action}")


LIST_CARDS_DEF = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "integer",
            "description": "Optional L1 domain code filter",
        },
    },
}


def handle_list_cards(args: dict, ctx: dict) -> ToolResult:
    db = ctx.get("db")
    namespace = ctx.get("namespace")
    if not db:
        return ToolResult(ok=False, error="Database not available")
    try:
        cards = list_card_index(domain=args.get("domain"), db=db, namespace=namespace)
        return ToolResult(ok=True, data={"cards": cards})
    except Exception as e:
        return ToolResult(ok=False, error=str(e))


GET_CARD_DETAIL_DEF = {
    "type": "object",
    "properties": {
        "flow_hash": {
            "type": "string",
            "description": "Flow hash of the card to expand",
        },
    },
    "required": ["flow_hash"],
}


def handle_get_card_detail(args: dict, ctx: dict) -> ToolResult:
    db = ctx.get("db")
    namespace = ctx.get("namespace")
    if not db:
        return ToolResult(ok=False, error="Database not available")
    try:
        detail = get_card_detail(args.get("flow_hash", ""), db=db, namespace=namespace)
        if detail:
            return ToolResult(ok=True, data=detail)
        return ToolResult(ok=False, error="Card not found")
    except Exception as e:
        return ToolResult(ok=False, error=str(e))
