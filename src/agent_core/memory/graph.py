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
    relation_type: str = "FOLLOWS"
    total_count: int = 0
    approved_count: int = 0
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
            relation_type=data.get("relation_type") or "FOLLOWS",
            total_count=int(data.get("total_count") or 0),
            approved_count=int(data.get("approved_count") or 0),
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


def _median(values: List[int]) -> float:
    if not values:
        return 0.0
    vs = sorted(int(v) for v in values)
    n = len(vs)
    mid = n // 2
    if n % 2 == 1:
        return float(vs[mid])
    return (float(vs[mid - 1]) + float(vs[mid])) / 2.0


def _prune_edges(edges: Dict[Tuple[str, str, str, str], EdgeStat]) -> int:
    if not edges:
        return 0
    counts = [int(e.total_count or 0) for e in edges.values()]
    med = _median(counts)
    threshold = med * 0.1
    if threshold <= 0:
        return 0
    removed = 0
    for k in list(edges.keys()):
        e = edges.get(k)
        if not e:
            continue
        if float(e.total_count or 0) < threshold:
            edges.pop(k, None)
            removed += 1
    if removed:
        logger.info("[AgentFlowGraph] pruned %s edges (median=%s, threshold=%s)", removed, med, threshold)
    return removed


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


def _detect_dependency(step_b: Dict[str, Any], step_a_num: int) -> bool:
    def _contains_placeholder(obj: Any, target_step: int) -> bool:
        if isinstance(obj, str):
            pattern = r"\{\{step" + str(target_step) + r"\.[^}]+\}\}"
            if re.search(pattern, obj):
                return True
        elif isinstance(obj, dict):
            for v in obj.values():
                if _contains_placeholder(v, target_step):
                    return True
        elif isinstance(obj, list):
            for item in obj:
                if _contains_placeholder(item, target_step):
                    return True
        return False

    path = step_b.get("path") or ""
    body = step_b.get("body")
    if _contains_placeholder(path, step_a_num):
        return True
    if body is not None and _contains_placeholder(body, step_a_num):
        return True
    return False


def update_graph_from_steps(
    steps: List[Dict[str, Any]], approved: bool,
    db: DatabasePort,
    namespace: Optional[str] = None,
) -> None:
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
        # 跳过 EXEC 步骤（shell 命令独有性强，没有模式复用价值）
        if from_m == "EXEC" or to_m == "EXEC":
            continue
        # 跳过自环边（source==target 会导致前端 g6 报 Edge already exists）
        if from_m == to_m and from_p == to_p:
            continue
        relation = "DEPENDS_ON" if _detect_dependency(b, i + 1) else "FOLLOWS"
        key = EdgeStat.key(from_m, from_p, to_m, to_p)
        edge = edges.get(key)
        if not edge:
            edge = EdgeStat(
                from_method=from_m, from_path=from_p,
                to_method=to_m, to_path=to_p,
                relation_type=relation,
                total_count=0, approved_count=0,
                created_at=now, updated_at=now,
            )
            edge.to_dict()["namespace"] = namespace
            edges[key] = edge
        else:
            if relation == "DEPENDS_ON" and edge.relation_type == "FOLLOWS":
                edge.relation_type = "DEPENDS_ON"
        edge.total_count += 1
        if approved:
            edge.approved_count += 1
        edge.updated_at = now
        changed = True

    if changed:
        _prune_edges(edges)
        _save_edges(edges, db)


def suggest_next_nodes(
    current_step: Dict[str, Any],
    top_k: int = 3,
    relation_filter: Optional[str] = None,
    db: Optional[DatabasePort] = None,
    namespace: Optional[str] = None,
) -> List[Dict[str, Any]]:
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
            if relation_filter and edge.relation_type != relation_filter:
                continue
            candidates.append(edge)

    if not candidates:
        return []

    def _score(e: EdgeStat) -> float:
        if e.total_count <= 0:
            return 0.0
        ratio = e.approved_count / float(e.total_count)
        relation_boost = 0.05 if e.relation_type == "DEPENDS_ON" else 0.0
        return ratio + relation_boost + 0.01 * min(e.total_count, 100)

    candidates.sort(key=_score, reverse=True)

    out: List[Dict[str, Any]] = []
    for e in candidates[:top_k]:
        ratio = e.approved_count / float(e.total_count) if e.total_count > 0 else 0.0
        out.append({
            "method": e.to_method,
            "path": e.to_path,
            "relation_type": e.relation_type,
            "total_count": e.total_count,
            "approved_ratio": round(ratio, 2),
        })
    return out
