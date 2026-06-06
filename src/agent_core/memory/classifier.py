import hashlib
import logging
import re
import struct
from dataclasses import dataclass
from typing import Any, Dict, Tuple

logger = logging.getLogger("agent_core")


def _hash32(s: str) -> int:
    """Deterministic 32-bit hash from string (sha256 -> first 4 bytes)"""
    return struct.unpack(">I", hashlib.sha256(s.encode()).digest()[:4])[0]


@dataclass
class OpCode:
    l1: int      # domain (8bit) - hash(protocol + server)
    l2: int      # action (8bit) - hash(tool_name)
    l3: int      # resource (32bit) - hash(tool_name + path_pattern)
    l4: int      # detail (32bit) - hash(tool_name + full_path)

    def level_code(self, level: int) -> int:
        # For L1 and L2, we use the full 32-bit hash
        # For L3/L4 we include parent levels for hierarchy
        if level == 1:
            return self.l1
        elif level == 2:
            return (self.l1 * 31) ^ self.l2
        elif level == 3:
            return (self.l1 * 31 * 31) ^ (self.l2 * 31) ^ self.l3
        elif level == 4:
            return (self.l1 * 31 * 31 * 31) ^ (self.l2 * 31 * 31) ^ (self.l3 * 31) ^ self.l4
        return self.l1


def _normalize_path(path: str) -> str:
    """Normalize path: remove query string, replace UUIDs with placeholder"""
    p = (path or "").strip()
    if not p:
        return ""
    p = p.split("?", 1)[0]
    uuid_pat = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    p = re.sub(uuid_pat, "{id}", p)
    # Normalize Windows paths: replace drive letter + backslashes
    p = re.sub(r"^[A-Za-z]:\\", "/", p)
    p = p.replace("\\", "/")
    return p


def classify(method: str, path: str, tool_name: str = "") -> OpCode:
    """Classify a step into 4-level OpCode.
    
    Args:
        method: HTTP method or EXEC
        path: API path or command
        tool_name: Full tool name (e.g. mcp__capture__get_task_list, read_file, terminal)
    """
    method = (method or "GET").upper()
    path = (path or "").strip()
    tool_name = (tool_name or method).strip()

    # MCP tools: mcp__{server}__{tool_name}
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        if len(parts) >= 3:
            l1_source = f"mcp__{parts[1]}"       # "mcp__capture"
            l2_source = tool_name                  # "mcp__capture__get_task_list"
        else:
            l1_source = tool_name
            l2_source = tool_name
    else:
        l1_source = method                         # "GET", "POST", "EXEC"
        l2_source = tool_name                      # "read_file", "terminal"

    l1 = _hash32(l1_source) & 0xFFFF       # 16-bit for readability
    l2 = _hash32(l2_source) & 0xFFFF
    l3 = _hash32(tool_name + _normalize_path(path)) & 0xFFFFFFFF
    l4 = _hash32(tool_name + path) & 0xFFFFFFFF

    return OpCode(l1=l1, l2=l2, l3=l3, l4=l4)


def decode_l1(code: int) -> int:
    """Extract L1 from a level code (works for level>=2 codes too)"""
    return code


def level_name(code: int, level: int) -> str:
    """Human-readable summary of a level code (for display only)"""
    if level == 1:
        return f"L1:{code:04x}"
    elif level == 2:
        return f"L2:{code:08x}"
    elif level == 3:
        return f"L3:{code:08x}"
    return f"L4:{code:08x}"
