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


@dataclass
class AgentMemoryCard:
    """记忆卡片统一模型。"""

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

    # 踩坑记录：LLM 在步骤失败/重试时生成
    pitfalls: List[str] = field(default_factory=list)
    # 经验总结：LLM 在累计执行 5 次时生成
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
            pitfalls=list(data.get("pitfalls") or []),
            experience_notes=list(data.get("experience_notes") or []),
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
    """记录一次成功的流程。

    Args:
        namespace: 多实例隔离标签,同一命名空间内共享记忆。
    """
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
    """检索与当前意图相似的历史流程。

    Args:
        namespace: 多实例隔离标签。
    """
    user_intent = (user_intent or "").strip()
    if not user_intent:
        return []

    if db is None:
        from agent_core.agent import _get_default_db
        db = _get_default_db()

    items = db.load_all_memory_cards(namespace=namespace)
    if not items:
        return []

    q_vec = get_embedding(user_intent, llm)
    if not q_vec:
        return []

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for it in items:
        card = AgentMemoryCard.from_dict(it)
        if scene_tag is not None and card.scene_tag and card.scene_tag != scene_tag:
            continue
        v = card.intent_vector or []
        sim = cosine(q_vec, v)
        scored.append((sim, card.to_dict()))
    scored.sort(key=lambda x: x[0], reverse=True)

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
                    new_items = prune_cards_by_tail_elimination(new_items)
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
    """根据用户反馈更新记忆卡片。"""
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
        new_items = prune_cards_by_tail_elimination(new_items)
        for it in new_items:
            db.save_memory_card(it)


def list_flows(db: DatabasePort, namespace: Optional[str] = None) -> List[Dict[str, Any]]:
    """返回当前所有记忆卡片（按 updated_at 降序）。"""
    items = db.load_all_memory_cards(namespace=namespace)
    cards = [AgentMemoryCard.from_dict(it) for it in items]
    cards.sort(key=lambda c: c.updated_at, reverse=True)
    return [c.to_dict() for c in cards]


# ──────────────────────────────────────────
# 踩坑提取 + 经验总结（需要 LLM）
# ──────────────────────────────────────────

def enrich_card_pitfalls(card: AgentMemoryCard, steps: list, llm: Optional[LLMPort]) -> list[str]:
    """检测失败/重试模式，需要时调 LLM 生成踩坑文本。

    流程规则:
      遍历 steps
        step[i].ok == False?
          -> step[i+1].ok == True AND 同 method+path? -> LLM 生成重试踩坑
          -> 否 -> LLM 生成失败踩坑

    Args:
        card: 要 enrich 的卡片
        steps: 本次执行的步骤列表
        llm: LLM 端口（None 时跳过）

    Returns:
        新生成的 pitfalls 列表（尚未写入 card）
    """
    if not steps or not llm:
        return []

    new_pitfalls = []
    intent = card.intent_summary

    for i in range(len(steps)):
        s = steps[i]
        if s.get("ok") is not False:
            continue

        # 分支: 失败后重试成功?
        if i + 1 < len(steps):
            nxt = steps[i + 1]
            if nxt.get("ok") and s.get("method") == nxt.get("method") and s.get("path") == nxt.get("path"):
                text = _llm_pitfall_retry(llm, intent, s, nxt)
                if text and text not in card.pitfalls:
                    new_pitfalls.append(text)
                continue

        # 分支: 普通失败
        text = _llm_pitfall_error(llm, intent, s)
        if text and text not in card.pitfalls:
            new_pitfalls.append(text)

    return new_pitfalls


def _llm_pitfall_error(llm: LLMPort, intent: str, step: dict) -> Optional[str]:
    """调 LLM 生成失败的踩坑文本"""
    step_num = step.get("step", "?")
    method = step.get("method", "")
    path = step.get("path", "")
    preview = str(step.get("result_preview") or "")[:200]
    error = str(step.get("error") or "")[:200]

    prompt = (
        f"你在执行「{intent}」流程时，以下步骤失败了:\n"
        f"步骤 {step_num}: {method} {path}\n"
        f"返回: {preview}\n"
        f"错误: {error}\n\n"
        f"请写出 1-2 句踩坑提醒（不超过 50 字，具体可操作）:"
    )
    try:
        resp = llm.chat([{"role": "user", "content": [{"type": "text", "text": prompt}]}])
        return resp.content.strip() if resp and resp.content else None
    except Exception:
        return None


def _llm_pitfall_retry(llm: LLMPort, intent: str, failed: dict, success: dict) -> Optional[str]:
    """调 LLM 生成重试踩坑文本"""
    step_num = failed.get("step", "?")
    method = failed.get("method", "")
    path = failed.get("path", "")
    fail_preview = str(failed.get("result_preview") or "")[:200]

    prompt = (
        f"你在执行「{intent}」流程时，以下步骤首次失败后重试成功:\n"
        f"步骤 {step_num}: {method} {path}\n"
        f"首次失败: {fail_preview}\n\n"
        f"请写出 1-2 句踩坑提醒（不超过 50 字，说明什么情况下会失败及如何避免）:"
    )
    try:
        resp = llm.chat([{"role": "user", "content": [{"type": "text", "text": prompt}]}])
        return resp.content.strip() if resp and resp.content else None
    except Exception:
        return None


def enrich_card_experience(card: AgentMemoryCard, llm: Optional[LLMPort]) -> Optional[str]:
    """累计执行 5 次时调 LLM 生成经验总结。

    流程规则:
      success_count >= 3 AND % 5 == 0? -> LLM 生成经验

    Args:
        card: 卡片
        llm: LLM 端口

    Returns:
        经验文本（尚未写入 card），或 None
    """
    if card.success_count < 3 or card.success_count % 5 != 0:
        return None
    if not llm:
        return None

    steps_summary = " -> ".join(
        f"{s.get('method','')} {s.get('path','')}" for s in (card.steps or [])
    )[:300]

    pitfalls_text = ""
    if card.pitfalls:
        pitfalls_text = "\n踩坑记录:\n" + "\n".join(f"- {p}" for p in card.pitfalls)

    prompt = (
        f"以下流程已成功执行 {card.success_count} 次:\n"
        f"意图: {card.intent_summary}\n"
        f"步骤: {steps_summary}{pitfalls_text}\n\n"
        f"请写出该流程的经验总结，包括:\n"
        f"- 标准操作顺序\n"
        f"- 需要特别注意的点\n"
        f"- 常见的变体或分支\n"
        f"不超过 100 字:"
    )
    try:
        resp = llm.chat([{"role": "user", "content": [{"type": "text", "text": prompt}]}])
        return resp.content.strip() if resp and resp.content else None
    except Exception:
        return None
