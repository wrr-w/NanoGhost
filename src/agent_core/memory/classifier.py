import hashlib
import logging
from dataclasses import dataclass
import struct
from typing import Any

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


def classify(method: str, path: str, tool_name: str = "") -> OpCode:
    """Classify a step into 4-level OpCode.

    Args:
        method: HTTP method or EXEC
        path: API path or command (raw, no normalization)
        tool_name: Full tool name
    """
    method = (method or "GET").upper()
    path = (path or "").strip()
    tool_name = (tool_name or method).strip()

    l1 = _hash32(method) & 0xFFFF
    l2 = _hash32(tool_name) & 0xFFFF
    l3 = _hash32(tool_name + path) & 0xFFFFFFFF
    l4 = _hash32(tool_name + path + method) & 0xFFFFFFFF

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
