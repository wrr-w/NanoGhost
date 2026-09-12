import asyncio
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from agent_core.memory.cards import record_successful_flow, enrich_card_experience
from agent_core.memory.graph import update_graph_ml
from agent_core.memory.intent import summarize_intent, append_to_memory_md, summarize_to_memory_md

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
    try:
        flow_hash = None

        intent = await asyncio.to_thread(
            summarize_intent,
            agent.db, session_id, agent.llm, user_message,
        )
        logger.info(f"[AgentMemory] Phase1: intent='{intent}' steps={len(all_steps_out)}")

        flow_hash = await asyncio.to_thread(
            record_successful_flow,
            intent, all_steps_out, step_counter[0],
            db=agent.db, llm=agent.llm, namespace=agent.namespace,
        )
        logger.info(f"[AgentMemory] Phase1 done: flow_hash={flow_hash}")

        logger.info(f"[AgentMemory] Phase2: update_graph_ml from {len(all_steps_out)} steps")
        await asyncio.to_thread(update_graph_ml, all_steps_out, db=agent.db, namespace=agent.namespace)
        logger.info("[AgentMemory] Phase2 done")

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
                    logger.info("[AgentMemory] Phase3: enriching experience")
                    exp = await asyncio.to_thread(enrich_card_experience, card, reply, agent.llm)
                    if exp:
                        logger.info(f"[AgentMemory] Phase3: got experience: {exp[:80]}")
                        if exp not in card.experience_notes:
                            card.experience_notes.append(exp)
                            await asyncio.to_thread(agent.db.save_memory_card, card.to_dict())
                            logger.info("[AgentMemory] Phase3: saved experience")
                    else:
                        logger.info("[AgentMemory] Phase3: no experience generated")
            except Exception as e2:
                logger.error(f"[AgentMemory] Phase3 enrich error: {e2}")

        # Phase 4: memory.md
        try:
            md_entries = await asyncio.to_thread(
                summarize_to_memory_md,
                agent.llm, user_message, reply, session_id, agent.db, step_counter[0],
            )
            if md_entries:
                logger.info(f"[AgentMemory] Phase4: {len(md_entries)} entries for memory.md")
                await asyncio.to_thread(append_to_memory_md, agent.db, agent.namespace, md_entries)
        except Exception as e4:
            logger.error(f"[AgentMemory] Phase4 error: {e4}")

    except Exception as e:
        logger.error(f"[AgentMemory] post-process error: {e}")
