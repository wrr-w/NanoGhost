import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from agent_core.interfaces import LLMPort

logger = logging.getLogger("agent_core")


def get_embedding(text: str, llm: Optional[LLMPort] = None) -> List[float]:
    """调用兼容 OpenAI Embedding 接口生成向量；失败时返回空列表。"""
    text = (text or "").strip()
    if not text:
        return []
    if llm is None:
        return []
    try:
        return llm.embed(text)
    except Exception as e:
        logger.error(f"[AgentMemory] embedding error: {e}")
        return []


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return s / (na * nb)


def mmr_select(
    candidates: List[Dict[str, Any]],
    query_vec: List[float],
    top_k: int = 3,
    lambda_mult: float = 0.7,
) -> List[Dict[str, Any]]:
    """基于向量的简单 MMR（多样性重排）。"""
    if not candidates or not query_vec:
        return []
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for m in candidates:
        v = m.get("intent_vector") or []
        sim = cosine(query_vec, v)
        scored.append((sim, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    scored = scored[: max(top_k * 4, top_k)]

    selected: List[Dict[str, Any]] = []
    selected_vecs: List[List[float]] = []
    while scored and len(selected) < top_k:
        best_idx = -1
        best_score = -1.0
        for idx, (sim_q, m) in enumerate(scored):
            v = m.get("intent_vector") or []
            if not selected_vecs:
                mmr_score = sim_q
            else:
                max_sim_sel = max(cosine(v, sv) for sv in selected_vecs)
                mmr_score = lambda_mult * sim_q - (1.0 - lambda_mult) * max_sim_sel
            if mmr_score > best_score:
                best_score = mmr_score
                best_idx = idx
        if best_idx < 0:
            break
        _, m = scored.pop(best_idx)
        selected.append(m)
        selected_vecs.append(m.get("intent_vector") or [])
    return selected
