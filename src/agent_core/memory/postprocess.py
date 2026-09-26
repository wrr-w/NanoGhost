import logging
import os
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from agent_core.memory.cards import record_successful_flow, enrich_card_experience
from agent_core.memory.files import append_daily_line
from agent_core.memory.graph import update_graph_ml
from agent_core.memory.intent import summarize_intent, append_to_memory_md, summarize_to_memory_md

if TYPE_CHECKING:
    from agent_core.engine.agent import Agent

logger = logging.getLogger("agent_core")


def postprocess_turn_sync(
    agent: "Agent",
    user_message: str,
    reply: str,
    all_steps_out: List[Dict[str, Any]],
    step_counter: List[int],
    session_id: Optional[str],
) -> None:
    """回合后处理（**同步**）。

    由 `MemoryPipeline` 的常驻线程调用，已经不在事件循环里，所以直接同步调用
    DB / 文件 / LLM，不再需要 `asyncio.to_thread` 包一层。
    任何异常都只记日志，绝不向上抛（不能影响用户回复）。
    """
    try:
        flow_hash = None

        # Phase 1: intent + card
        intent = summarize_intent(agent.db, session_id, agent.llm, user_message)
        logger.info(f"[AgentMemory] Phase1: intent='{intent}' steps={len(all_steps_out)}")

        flow_hash = record_successful_flow(
            intent, all_steps_out, step_counter[0],
            db=agent.db, llm=agent.llm, namespace=agent.namespace,
        )
        logger.info(f"[AgentMemory] Phase1 done: flow_hash={flow_hash}")

        # Phase 2: graph
        logger.info(f"[AgentMemory] Phase2: update_graph_ml from {len(all_steps_out)} steps")
        update_graph_ml(all_steps_out, db=agent.db, namespace=agent.namespace)
        logger.info("[AgentMemory] Phase2 done")

        # Phase 3: experience (每个 flow 只调 1 次 LLM)
        if flow_hash and agent.llm:
            try:
                items = agent.db.load_all_memory_cards(namespace=agent.namespace)
                card_dict = None
                for it in items:
                    if it.get("flow_hash") == flow_hash:
                        card_dict = it
                        break
                if card_dict:
                    from agent_core.memory.cards import AgentMemoryCard
                    card = AgentMemoryCard.from_dict(card_dict)
                    logger.info("[AgentMemory] Phase3: enriching experience")
                    exp = enrich_card_experience(card, reply, agent.llm)
                    if exp:
                        logger.info(f"[AgentMemory] Phase3: got experience: {exp[:80]}")
                        if exp not in card.experience_notes:
                            card.experience_notes.append(exp)
                            agent.db.save_memory_card(card.to_dict())
                            logger.info("[AgentMemory] Phase3: saved experience")
                    else:
                        logger.info("[AgentMemory] Phase3: no experience generated")
            except Exception as e2:
                logger.error(f"[AgentMemory] Phase3 enrich error: {e2}")

        # Phase 4: memory.md（长期）
        try:
            md_entries = summarize_to_memory_md(
                agent.llm, user_message, reply, session_id, agent.db, step_counter[0],
            )
            if md_entries:
                logger.info(f"[AgentMemory] Phase4: {len(md_entries)} entries for memory.md")
                append_to_memory_md(agent.db, agent.namespace, md_entries)
        except Exception as e4:
            logger.error(f"[AgentMemory] Phase4 error: {e4}")

        # Phase 5: memory.daily（当日工作记忆，长期/当日分层的写入侧）
        try:
            inst_dir = os.getenv("INSTANCE_DIR", "")
            if inst_dir:
                line = f"- {time.strftime('%H:%M')} {intent or (user_message or '')[:60]}"
                append_daily_line(inst_dir, line)
                logger.info("[AgentMemory] Phase5: daily memory appended")
        except Exception as e5:
            logger.error(f"[AgentMemory] Phase5 error: {e5}")

    except Exception as e:
        logger.error(f"[AgentMemory] post-process error: {e}")


async def postprocess_turn(
    agent: "Agent",
    user_message: str,
    reply: str,
    all_steps_out: List[Dict[str, Any]],
    step_counter: List[int],
    session_id: Optional[str],
) -> None:
    """异步兼容层：把同步实现丢到线程池执行（保留给外部 await 调用）。"""
    import asyncio

    await asyncio.to_thread(
        postprocess_turn_sync,
        agent, user_message, reply, all_steps_out, step_counter, session_id,
    )
