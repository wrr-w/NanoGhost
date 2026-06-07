import json
import logging
import time
from typing import Any, Dict, List, Optional

from agent_core.interfaces import DatabasePort

logger = logging.getLogger("agent_core")


def update_graph_ml(
    steps: List[Dict[str, Any]],
    db: DatabasePort,
    namespace: Optional[str] = None,
) -> None:
    """v3 Graph 写入：使用多层 OpCode 编码（L1~L4）。"""
    if not steps or len(steps) < 2:
        return

    from agent_core.memory.classifier import classify

    now = time.time()
    for i in range(len(steps) - 1):
        a = steps[i]
        b = steps[i + 1]
        method_a = (a.get("method") or "GET").upper()
        path_a = (a.get("path") or "").strip()
        tool_a = (a.get("tool_name") or method_a).strip()
        method_b = (b.get("method") or "GET").upper()
        path_b = (b.get("path") or "").strip()
        tool_b = (b.get("tool_name") or method_b).strip()

        if method_a == method_b and path_a == path_b:
            continue

        code_a = classify(method_a, path_a, tool_a)
        code_b = classify(method_b, path_b, tool_b)

        for level in [1, 2, 3, 4]:
            edge = {
                "level": level,
                "from_code": code_a.level_code(level),
                "to_code": code_b.level_code(level),
                "total_count": 1,
                "namespace": namespace,
                "created_at": now,
                "updated_at": now,
            }
            try:
                db.save_ml_edge(edge)
            except Exception as e:
                logger.error(f"[AgentFlowGraph] save_ml_edge error: {e}")
                return
