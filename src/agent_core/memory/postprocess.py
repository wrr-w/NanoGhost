import asyncio
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from agent_core.memory.cards import record_successful_flow, enrich_card_experience
from agent_core.memory.graph import update_graph_ml
from agent_core.memory.intent import summarize_intent, extract_memory_md_entries, append_to_memory_md

if TYPE_CHECKING:
    from agent_core.engine.agent import Agent

logger = logging.getLogger("agent_core")


async def postprocess_turn(
    agent: "Agent",
    user_message: str,
    reply: str,
    all_steps_out: List[Dict[str, Any]],
    step_counter: List[int],
    session_id: Optional[str],
) -> None:
    """回合结束后触发的记忆后处理序列（不阻塞用户回复）。

    Phase 1: 写入 Card (record_successful_flow)
    Phase 2: 写入 Graph (update_graph_ml)
    Phase 3: LLM 总结 experience (enrich_card_experience)
    Phase 4: Agent 通过 memory_write 工具写入 memory.md
    """
    try:
        flow_hash = None
        if all_steps_out:
            intent = await asyncio.to_thread(
                summarize_intent,
                agent.db, session_id, agent.llm, user_message,
            )
            flow_hash = await asyncio.to_thread(
                record_successful_flow,
                intent, all_steps_out, step_counter[0],
                db=agent.db, llm=agent.llm, namespace=agent.namespace,
            )
            await asyncio.to_thread(update_graph_ml, all_steps_out, db=agent.db, namespace=agent.namespace)

            if flow_hash and agent.llm:
                try:
                    items = await asyncio.to_thread(agent.db.load_all_memory_cards, namespace=agent.namespace)
                    card_dict = None
                    for it in items:
                        if it.get("flow_hash") == flow_hash:
                            card_dict = it
                            break
                    if card_dict:
                        from agent_core.memory.cards import AgentMemoryCard
                        card = AgentMemoryCard.from_dict(card_dict)
                        exp = await asyncio.to_thread(enrich_card_experience, card, reply, agent.llm)
                        if exp and exp not in card.experience_notes:
                            card.experience_notes.append(exp)
                            await asyncio.to_thread(agent.db.save_memory_card, card.to_dict())
                except Exception as e2:
                    logger.error(f"[AgentMemory] enrich error: {e2}")

            try:
                memory_entries = extract_memory_md_entries(
                    user_message, reply, all_steps_out
                )
                if memory_entries:
                    await asyncio.to_thread(append_to_memory_md, agent.db, agent.namespace, memory_entries)
            except Exception as e3:
                logger.error(f"[AgentMemory] memory.md error: {e3}")

    except Exception as e:
        logger.error(f"[AgentMemory] post-process error: {e}")
