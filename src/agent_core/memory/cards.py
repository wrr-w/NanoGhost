import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from agent_core.interfaces import DatabasePort, LLMPort
from agent_core.memory.embedding import get_embedding, cosine, mmr_select


logger = logging.getLogger("agent_core")


def _median(values: List[int]) -> float:
    if not values:
        return 0.0
    vs = sorted(int(v) for v in values)
    n = len(vs)
    mid = n // 2
    if n % 2 == 1:
        return float(vs[mid])
    return (float(vs[mid - 1]) + float(vs[mid])) / 2.0


def _prune_cards_by_tail_elimination(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not items:
        return items
    cards = [AgentMemoryCard.from_dict(it) for it in items]
    hits = [int(c.success_count or 0) for c in cards]
    med = _median(hits)
    threshold = med * 0.1
    if threshold <= 0:
        return items
    kept: List[Dict[str, Any]] = []
    removed = 0
    for c in cards:
        if int(c.approved_count or 0) > 0:
            kept.append(c.to_dict())
            continue
        if float(c.success_count or 0) < threshold:
            removed += 1
            continue
        kept.append(c.to_dict())
    if removed:
        logger.info("[AgentMemory] pruned %s cards (median=%s, threshold=%s)", removed, med, threshold)
    return kept

MEMORY_MIN_SIM = 0.4
MEMORY_MAX_REJECTIONS_BEFORE_DELETE = 3


def _safe_list(val):
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return list(parsed) if isinstance(parsed, list) else [parsed]
        except Exception:
            return [val]
    return list(val) if hasattr(val, "__iter__") else [val]


@dataclass
class AgentMemoryCard:
    id: str
    flow_hash: str
    intent_summary: str
    intent_examples: List[str]
    intent_vector: List[float]
    flow_signature: Dict[str, Any]
    steps: List[Dict[str, Any]]
    success_count: int
    total_rounds: int
    created_at: float
    updated_at: float
    approved_count: int = 0
    rejected_count: int = 0
    trigger_count: int = 0
    scene_tag: Optional[str] = None
    namespace: Optional[str] = None

    experience_notes: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentMemoryCard":
        return cls(
            id=data.get("id") or str(uuid.uuid4()),
            flow_hash=data.get("flow_hash") or "",
            intent_summary=data.get("intent_summary") or "",
            intent_examples=list(data.get("intent_examples") or []),
            intent_vector=list(data.get("intent_vector") or []),
            flow_signature=data.get("flow_signature") or {},
            steps=list(data.get("steps") or []),
            success_count=int(data.get("success_count") or 0),
            total_rounds=int(data.get("total_rounds") or 0),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            approved_count=int(data.get("approved_count") or 0),
            rejected_count=int(data.get("rejected_count") or 0),
            trigger_count=int(data.get("trigger_count") or 0),
            scene_tag=data.get("scene_tag"),
            namespace=data.get("namespace"),
            experience_notes=_safe_list(data.get("experience_notes")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _normalize_path_for_memory(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return ""
    p = p.split("?", 1)[0].strip()
    uuid_pat = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    p = re.sub(rf"^(/api/tasks/){uuid_pat}(/.*)?$", r"\1{task_id}\2", p)
    p = re.sub(rf"^(/api/items/){uuid_pat}(/.*)?$", r"\1{item_id}\2", p)
    p = re.sub(rf"/{uuid_pat}(?=/|$)", "/{id}", p)
    return p


def _flow_signature(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    sig_steps: List[str] = []
    for s in steps or []:
        method = (s.get("method") or "").upper()
        path = _normalize_path_for_memory(s.get("path") or "")
        sig_steps.append(f"{method} {path}")
    return {"steps": sig_steps, "length": len(sig_steps)}


def _slim_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    slim: List[Dict[str, Any]] = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        path_only = _normalize_path_for_memory(s.get("path") or "")
        slim.append({
            "step": int(s.get("step") or 0),
            "method": (s.get("method") or "").upper(),
            "path": path_only,
            "ok": bool(s.get("ok") if s.get("ok") is not None else True),
            "status_code": int(s.get("status_code") or 0),
        })
    return slim


def record_successful_flow(
    user_intent: str,
    steps: List[Dict[str, Any]],
    rounds_used: int,
    db: DatabasePort,
    llm: Optional[LLMPort] = None,
    namespace: Optional[str] = None,
) -> Optional[str]:
    user_intent = (user_intent or "").strip()
    if not user_intent or not steps:
        return None

    slim_steps = _slim_steps(steps)
    has_success = any(s.get("ok", True) for s in slim_steps)
    if not has_success:
        logger.info("[AgentMemory] skip recording flow: all steps failed")
        return None

    sig = _flow_signature(slim_steps)
    sig_text = "|".join(sig.get("steps") or [])
    flow_hash = hashlib.sha256(sig_text.encode("utf-8")).hexdigest()[:16]

    items = db.load_all_memory_cards(namespace=namespace)
    now = time.time()

    existing = None
    for it in items:
        if it.get("flow_hash") == flow_hash:
            existing = it
            break

    if existing:
        examples: List[str] = existing.get("intent_examples") or []
        examples.append(user_intent)
        seen = set()
        uniq_examples: List[str] = []
        for e in examples:
            e = (e or "").strip()
            if not e or e in seen:
                continue
            seen.add(e)
            uniq_examples.append(e)

        example_vecs: List[Tuple[str, List[float]]] = []
        for e in uniq_examples:
            v = get_embedding(e, llm)
            if v:
                example_vecs.append((e, v))
        if example_vecs:
            q_vec = [0.0] * len(example_vecs[0][1])
            for _, v in example_vecs:
                for i, x in enumerate(v):
                    q_vec[i] += x
            cnt = float(len(example_vecs))
            if cnt > 0:
                q_vec = [x / cnt for x in q_vec]
            cand = [{"intent_text": e, "intent_vector": v} for e, v in example_vecs]
            picked = mmr_select(cand, q_vec, top_k=3, lambda_mult=0.7)
            existing["intent_examples"] = [c["intent_text"] for c in picked]
            if picked:
                dim = len(picked[0]["intent_vector"])
                avg = [0.0] * dim
                for c in picked:
                    v = c["intent_vector"]
                    for i, x in enumerate(v):
                        avg[i] += x
                existing["intent_vector"] = [x / len(picked) for x in avg]

        existing["success_count"] = int(existing.get("success_count") or 0) + 1
        existing["total_rounds"] = int(existing.get("total_rounds") or 0) + max(1, int(rounds_used or 1))
        existing.setdefault("approved_count", 0)
        existing.setdefault("rejected_count", 0)
        existing.setdefault("trigger_count", 0)
        existing["updated_at"] = now
    else:
        emb = get_embedding(user_intent, llm)
        card = AgentMemoryCard(
            id=str(uuid.uuid4()),
            flow_hash=flow_hash,
            intent_summary=user_intent,
            intent_examples=[user_intent],
            intent_vector=emb,
            flow_signature=sig,
            steps=slim_steps,
            success_count=1,
            total_rounds=max(1, int(rounds_used or 1)),
            created_at=now,
            updated_at=now,
            approved_count=0,
            rejected_count=0,
            trigger_count=0,
            scene_tag=None,
            namespace=namespace,
        )
        items.append(card.to_dict())

        items = _prune_cards_by_tail_elimination(items)
    for it in items:
        db.save_memory_card(it)
    return flow_hash


def retrieve_similar_flows(
    user_intent: str,
    top_k: int = 3,
    scene_tag: Optional[str] = None,
    *,
    increment_trigger: bool = False,
    db: Optional[DatabasePort] = None,
    llm: Optional[LLMPort] = None,
    namespace: Optional[str] = None,
) -> List[Dict[str, Any]]:
    user_intent = (user_intent or "").strip()
    if not user_intent:
        return []

    if db is None:
        from agent_core.agent import _get_default_db
        db = _get_default_db()

    _t_db = time.time()
    items = db.load_all_memory_cards(namespace=namespace)
    logger.info(f"[AgentMemory] load_all_memory_cards took={time.time()-_t_db:.3f}s, count={len(items)}")
    if not items:
        return []

    _t_emb = time.time()
    q_vec = get_embedding(user_intent, llm)
    logger.info(f"[AgentMemory] get_embedding took={time.time()-_t_emb:.3f}s, len={len(q_vec) if q_vec else 0}")
    if not q_vec:
        return []

    _t_loop = time.time()
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for it in items:
        _scene_tag = it.get("scene_tag")
        if scene_tag is not None and _scene_tag and _scene_tag != scene_tag:
            continue
        v = it.get("intent_vector") or []
        sim = cosine(q_vec, v)
        scored.append((sim, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    logger.info(f"[AgentMemory] cosine loop took={time.time()-_t_loop:.3f}s, items={len(items)}, scored={len(scored)}")

    raw_candidates: List[Dict[str, Any]] = []
    for sim, m in scored[: max(top_k * 8, top_k)]:
        if sim <= 0.0 or sim < MEMORY_MIN_SIM:
            continue
        mm = dict(m)
        mm["_similarity"] = float(sim)
        succ = int(mm.get("success_count") or 0)
        approved = int(mm.get("approved_count") or 0)
        rejected = int(mm.get("rejected_count") or 0)
        total_fb = approved + rejected
        if total_fb > 0:
            acc_score = approved / float(total_fb)
        else:
            acc_score = succ
        mm["_acceptance_score"] = float(acc_score)
        raw_candidates.append(mm)

    if not raw_candidates:
        return []

    picked = mmr_select(raw_candidates, q_vec, top_k=top_k, lambda_mult=0.7)

    def _sort_key(m: Dict[str, Any]) -> Tuple[float, float]:
        return float(m.get("_acceptance_score") or 0.0), float(m.get("_similarity") or 0.0)
    picked.sort(key=_sort_key, reverse=True)

    if increment_trigger and picked:
        try:
            picked_hashes = {
                str(m.get("flow_hash") or "").strip()
                for m in picked if isinstance(m, dict)
            }
            picked_hashes.discard("")
            if picked_hashes:
                changed = False
                new_items: List[Dict[str, Any]] = []
                for it in items:
                    card = AgentMemoryCard.from_dict(it)
                    if card.flow_hash in picked_hashes:
                        card.trigger_count += 1
                        card.updated_at = time.time()
                        new_items.append(card.to_dict())
                        changed = True
                    else:
                        new_items.append(it)
                if changed:
                    new_items = _prune_cards_by_tail_elimination(new_items)
                    for it in new_items:
                        db.save_memory_card(it)
        except Exception as e:
            logger.error(f"[AgentMemory] increment_trigger failed: {e}")

    return picked


def record_memory_feedback(
    flow_hash: str,
    feedback: str,
    db: DatabasePort,
    namespace: Optional[str] = None,
) -> None:
    flow_hash = (flow_hash or "").strip()
    if not flow_hash:
        return
    feedback = (feedback or "").strip().lower()
    if feedback not in ("approved", "rejected"):
        return

    items = db.load_all_memory_cards(namespace=namespace)
    changed = False
    new_items: List[Dict[str, Any]] = []

    for it in items:
        card = AgentMemoryCard.from_dict(it)
        if card.flow_hash != flow_hash:
            new_items.append(it)
            continue
        card.trigger_count += 1
        if feedback == "approved":
            card.approved_count += 1
        elif feedback == "rejected":
            card.rejected_count += 1

        if (
            card.trigger_count >= MEMORY_MAX_REJECTIONS_BEFORE_DELETE
            and card.approved_count == 0
            and card.rejected_count >= MEMORY_MAX_REJECTIONS_BEFORE_DELETE
        ):
            logger.info(
                "[AgentMemory] delete flow %s due to repeated rejections",
                card.flow_hash,
            )
            changed = True
            continue
        card.updated_at = time.time()
        new_items.append(card.to_dict())
        changed = True

    if changed:
        new_items = _prune_cards_by_tail_elimination(new_items)
        for it in new_items:
            db.save_memory_card(it)


def list_flows(db: DatabasePort, namespace: Optional[str] = None) -> List[Dict[str, Any]]:
    items = db.load_all_memory_cards(namespace=namespace)
    cards = [AgentMemoryCard.from_dict(it) for it in items]
    cards.sort(key=lambda c: c.updated_at, reverse=True)
    return [c.to_dict() for c in cards]


def list_card_index(
    domain: Optional[int] = None,
    db: Optional[DatabasePort] = None,
    namespace: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if db is None:
        from agent_core.agent import _get_default_db
        db = _get_default_db()
    items = db.load_all_memory_cards(namespace=namespace)
    out = []
    for it in items:
        if domain is not None:
            card_l1 = it.get("l1_code") or 0
            if card_l1 != domain:
                continue
        out.append({
            "flow_hash": it.get("flow_hash"),
            "intent_summary": (it.get("intent_summary") or "")[:100],
            "l1_code": it.get("l1_code") or 0,
            "success_count": it.get("success_count") or 0,
        })
    return out


def get_card_detail(
    flow_hash: str,
    db: Optional[DatabasePort] = None,
    namespace: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if db is None:
        from agent_core.agent import _get_default_db
        db = _get_default_db()
    items = db.load_all_memory_cards(namespace=namespace)
    for it in items:
        if it.get("flow_hash") == flow_hash:
            card = AgentMemoryCard.from_dict(it)
            return {
                "flow_hash": card.flow_hash,
                "intent_summary": card.intent_summary,
                "steps": [{"method": s.get("method"), "path": s.get("path")} for s in (card.steps or [])],
                "experience_notes": card.experience_notes,
                "success_count": card.success_count,
            }
    return None

def enrich_card_experience(card: AgentMemoryCard, reply: str, llm: Optional[LLMPort]) -> Optional[str]:
    # v3: always summarize experience after each flow. No threshold.
    # Input: intent + steps + reply. LLM decides if anything worth noting.
    if not llm:
        return None

    steps_summary = " -> ".join(
        f"{s.get('method','')} {s.get('path','')}" for s in (card.steps or [])
    )[:300]

    prompt_lines = ["Task completed:"]
    prompt_lines.append(f"Intent: {card.intent_summary}")
    prompt_lines.append(f"Steps: {steps_summary}")
    prompt_lines.append("")
    prompt_lines.append("Summarize the experience in 1-2 sentences. Include any pitfalls,")
    prompt_lines.append("tips, or standard procedures worth remembering.")
    prompt_lines.append("If nothing notable, output nothing.")
    prompt = chr(10).join(prompt_lines)

    try:
        resp = llm.chat([{"role": "user", "content": [{"type": "text", "text": prompt}]}])
        return resp.content.strip() if resp and resp.content else None
    except Exception:
        return None
