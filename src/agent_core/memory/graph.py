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
    if not steps or len(steps) < 2:
        logger.info(f"[AgentMemory][Graph] skip: need >=2 steps, got {len(steps) if steps else 0}")
        return

    from agent_core.memory.classifier import classify

    now = time.time()
    edge_count = 0
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

        try:
            code_a = classify(method_a, path_a, tool_a)
            code_b = classify(method_b, path_b, tool_b)
        except Exception as e:
            logger.error(f"[AgentMemory][Graph] classify error at step {i}: {e}")
            continue

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
                edge_count += 1
            except Exception as e:
                logger.error(f"[AgentMemory][Graph] save_ml_edge error at step {i} level {level}: {e}")
    logger.info(f"[AgentMemory][Graph] saved {edge_count} edges from {len(steps)} steps")
