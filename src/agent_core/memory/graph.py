import json
import logging
import re
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

from agent_core.interfaces import DatabasePort

logger = logging.getLogger("agent_core")


@dataclass
class EdgeStat:
    from_method: str
    from_path: str
    to_method: str
    to_path: str
    total_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def key(cls, fm: str, fp: str, tm: str, tp: str) -> Tuple[str, str, str, str]:
        return fm.upper(), fp, tm.upper(), tp

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EdgeStat":
        return cls(
            from_method=(data.get("from_method") or "").upper(),
            from_path=data.get("from_path") or "",
            to_method=(data.get("to_method") or "").upper(),
            to_path=data.get("to_path") or "",
            total_count=int(data.get("total_count") or 0),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _load_edges(db: DatabasePort, namespace: Optional[str] = None) -> Dict[Tuple[str, str, str, str], EdgeStat]:
    try:
        rows = db.load_all_memory_edges(namespace=namespace)
        out: Dict[Tuple[str, str, str, str], EdgeStat] = {}
        for row in rows:
            edge = EdgeStat.from_dict(row)
            out[EdgeStat.key(edge.from_method, edge.from_path, edge.to_method, edge.to_path)] = edge
        return out
    except Exception as e:
        logger.error(f"[AgentFlowGraph] load error: {e}")
        return {}


def _save_edges(edges: Dict[Tuple[str, str, str, str], EdgeStat], db: DatabasePort) -> None:
    try:
        for edge in edges.values():
            db.save_memory_edge(edge.to_dict())
    except Exception as e:
        logger.error(f"[AgentFlowGraph] save error: {e}")


def _normalize_path_for_graph(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return ""
    p = p.split("?", 1)[0].strip()
    uuid_pat = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    p = re.sub(rf"^(/api/tasks/){uuid_pat}(/.*)?$", r"\1{task_id}\2", p)
    p = re.sub(rf"^(/api/items/){uuid_pat}(/.*)?$", r"\1{item_id}\2", p)
    p = re.sub(rf"/{uuid_pat}(?=/|$)", "/{id}", p)
    return p


def _normalize_step(step: Dict[str, Any]) -> Tuple[str, str]:
    method = (step.get("method") or "GET").upper()
    path = _normalize_path_for_graph(step.get("path") or "")
    return method, path


def _is_valid_node(method: str, path: str) -> bool:
    if not method or not path:
        return False
    p = path.strip()
    if not p or p == "/" or p.startswith("//"):
        return False
    segments = [s for s in p.split("/") if s]
    if not segments:
        return False
    return True


def update_graph_from_steps(
    steps: List[Dict[str, Any]],
    db: DatabasePort,
    namespace: Optional[str] = None,
) -> None:
    # v3: only count transitions, no scoring/pruning/relation_type
    if not steps or len(steps) < 2:
        return

    edges = _load_edges(db, namespace=namespace)
    now = time.time()
    changed = False

    for i in range(len(steps) - 1):
        a = steps[i]
        b = steps[i + 1]
        from_m, from_p = _normalize_step(a)
        to_m, to_p = _normalize_step(b)
        if not _is_valid_node(from_m, from_p) or not _is_valid_node(to_m, to_p):
            continue
        if from_m == to_m and from_p == to_p:
            continue
        key = EdgeStat.key(from_m, from_p, to_m, to_p)
        edge = edges.get(key)
        if not edge:
            edge = EdgeStat(
                from_method=from_m, from_path=from_p,
                to_method=to_m, to_path=to_p,
                total_count=0,
                created_at=now, updated_at=now,
            )
            edges[key] = edge
        edge.total_count += 1
        edge.updated_at = now
        changed = True

    if changed:
        _save_edges(edges, db)
def update_graph_ml(
    steps: List[Dict[str, Any]],
    db: DatabasePort,
    namespace: Optional[str] = None,
) -> None:
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



def query_outgoing_edges(
    current_step: Dict[str, Any],
    top_k: int = 10,
    db: Optional[DatabasePort] = None,
    namespace: Optional[str] = None,
) -> List[Dict[str, Any]]:
    # v3: list all outgoing edges, no scoring/recommendation
    if db is None:
        from agent_core.agent import _get_default_db
        db = _get_default_db()

    method, path = _normalize_step(current_step)
    edges = _load_edges(db, namespace=namespace)
    candidates: List[EdgeStat] = []

    for key, edge in edges.items():
        if edge.from_method == method and edge.from_path == path:
            if not _is_valid_node(edge.to_method, edge.to_path):
                continue
            candidates.append(edge)

    if not candidates:
        return []

    # Sorted by count ascending so LLM reads the raw numbers
    candidates.sort(key=lambda e: e.total_count, reverse=True)

    out: List[Dict[str, Any]] = []
    for e in candidates[:top_k]:
        out.append({
            "method": e.to_method,
            "path": e.to_path,
            "total_count": e.total_count,
        })
    return out
