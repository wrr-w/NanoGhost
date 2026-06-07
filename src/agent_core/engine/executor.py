"""
Agent 决策执行引擎。

包含核心的 LLM 循环逻辑（120 轮 tool-calling 决策），
以及 shell 命令执行 helper。
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple
from collections.abc import AsyncIterator

from agent_core.config import AgentConfig
from .messages import build_agent_messages_with_history
from agent_core.tool.models import ToolResult
from agent_core.memory.postprocess import postprocess_turn

logger = logging.getLogger("agent_core")

_MAX_ROUNDS = 120


# ──────────────────────────────────────────
# Shell 命令执行
# ──────────────────────────────────────────

def _execute_shell_command(
    command: str,
    step_num: int,
    timeout: int = 30,
    workdir: Optional[str] = None,
) -> Tuple[Dict, bool, Optional[str]]:
    """执行本地 shell 命令。"""
    logger.info(f"[ShellExec] step {step_num}: {command[:200]}")
    proc = None
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir or os.getcwd(),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        ok = proc.returncode == 0
        stdout = (stdout or b"").decode("utf-8", errors="replace")
        stderr = (stderr or b"").decode("utf-8", errors="replace")
        preview = ""
        if stdout:
            preview = stdout[:4000]
            if len(stdout) > 4000:
                preview += "\n…（输出已截断）"
        if stderr:
            if preview:
                preview += "\n--- stderr ---\n"
            preview += stderr[:2000]
            if len(stderr) > 2000:
                preview += "\n…（stderr 已截断）"
        step_out = {
            "step": step_num, "method": "EXEC", "path": command,
            "ok": ok, "exit_code": proc.returncode, "result_preview": preview,
        }
        return step_out, ok, None if ok else f"exit code {proc.returncode}"
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                proc.kill()
                if os.name == "nt":
                    import subprocess as _sp
                    _sp.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=5)
                proc.wait(timeout=5)
            except Exception:
                pass
        return {"step": step_num, "method": "EXEC", "path": command, "ok": False, "error": f"命令超时（{timeout}秒）", "exit_code": -1}, False, f"命令超时（{timeout}秒）"
    except FileNotFoundError as e:
        return {"step": step_num, "method": "EXEC", "path": command, "ok": False, "error": f"命令未找到: {e}"}, False, str(e)
    except Exception as e:
        return {"step": step_num, "method": "EXEC", "path": command, "ok": False, "error": str(e)}, False, str(e)


# ──────────────────────────────────────────
# AgentExecutor
# ──────────────────────────────────────────

class AgentExecutor:
    """执行 Agent 决策循环。

    持有 Agent 实例引用，通过其端口完成 LLM 调用和工具分发。
    """

    def __init__(self, agent):
        self.agent = agent

    async def run(
        self,
        user_message: str,
        session_id: Optional[str],
        config: AgentConfig,
        images: Optional[List[str]] = None,
    ) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
        """流式 Agent 对话。

        Args:
            user_message: 用户输入文本
            session_id: 会话 ID（None 表示不持久化）
            config: Agent 配置（base_url, sys_prompt, api_spec）
            images: 图片 Base64 列表

        Yields:
            (event_type, event_data) 事件对
        """
        final_reply = ""
        last_text_stream = ""
        step_counter = [0]

        root_id = getattr(config, "root_id", None)

        # ---- 图片入库 ----
        stored_image_ids = []
        if session_id and images and self.agent.image_port:
            try:
                for img_base64 in images:
                    img_id = await asyncio.to_thread(self.agent.image_port.add_image, img_base64)
                    stored_image_ids.append(img_id)
                    await asyncio.to_thread(self.agent.db.add_agent_message, session_id, "user", img_id, type="image", root_id=root_id)
            except Exception as e:
                logger.error(f"[Agent] save user message error: {e}")

        if stored_image_ids:
            yield ("images_stored", {"image_ids": stored_image_ids})

        # ---- 文本入库 ----
        if session_id and user_message:
            await asyncio.to_thread(self.agent.db.add_agent_message, session_id, "user", user_message, type="text", root_id=root_id)

        # ---- 构建消息 ----
        _t_messages = time.time()
        messages = await asyncio.to_thread(
            build_agent_messages_with_history,
            session_id=session_id,
            sys_prompt=config.sys_prompt,
            user_message=user_message,
            db=self.agent.db,
            user_images=images,
            image_urls=stored_image_ids,
            llm=self.agent.llm,
            namespace=self.agent.namespace,
            history_max_messages=getattr(config, "history_max_messages", 120),
            history_max_tokens=getattr(config, "history_max_tokens", 200_000),
            root_id=getattr(config, "root_id", None),
        )
        logger.info(f"[Agent] build_agent_messages_with_history 耗时={time.time()-_t_messages:.1f}s")

        # ---- 注入 SKILL.md 技能索引 ----
        _t_skill = time.time()
        skill_block = self.agent.skill_registry.build_skill_context()
        if skill_block:
            logger.info(f"[Agent] build_skill_context 耗时={time.time()-_t_skill:.1f}s")
        if skill_block:
            messages.append({
                "role": "system",
                "content": [{"type": "text", "text": skill_block}],
            })

        logger.debug(f"[Agent] final messages count={len(messages)}")
        for idx, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", [])
            content_types = []
            for c in content:
                if isinstance(c, dict):
                    content_types.append(c.get("type", "unknown"))
            logger.debug(f"[Agent] msg[{idx}] role={role}, content_types={content_types}")

        all_step_results: Dict[int, Dict] = {}
        all_steps_out: List[Dict[str, Any]] = []

        yield ("session", {"session_id": session_id})

        # ---- Tool 上下文 ----
        def _yield_event(ev_type: str, ev_data: Dict[str, Any]) -> None:
            _pending_events.append((ev_type, ev_data))

        # ---- 决策循环 ----
        for _round in range(_MAX_ROUNDS):
            yield ("status", {"message": "思考中…"})

            _pending_events: List[Tuple[str, Dict[str, Any]]] = []

            tool_context = {
                "db": self.agent.db,
                "llm": self.agent.llm,
                "image_port": self.agent.image_port,
                "config": config,
                "agent": self.agent,
                "session_id": session_id,
                "namespace": self.agent.namespace,
                "user_message": user_message,
                "step_counter": step_counter,
                "all_steps_out": all_steps_out,
                "all_step_results": all_step_results,
                "yield_event": _yield_event,
                "delegate_depth": 0,
            }

            # ---- 调用 LLM ----
            _t_schemas = time.time()
            tools_schemas = self.agent.tool_registry.get_available_schemas()
            logger.info(f"[Agent] get_available_schemas 耗时={time.time()-_t_schemas:.1f}s")
            try:
                for r in self.agent._hook_bus.emit("before_llm_call", messages=messages, config=config):
                    if r is not None:
                        messages = r
                _t_llm = time.time()
                response = await asyncio.to_thread(self.agent.llm.chat, messages, temperature=0.1, tools=tools_schemas)
                logger.info(f"[Agent] LLM call 耗时={time.time()-_t_llm:.1f}s")
                for r in self.agent._hook_bus.emit("after_llm_call", response=response, messages=messages):
                    if r is not None:
                        response = r
            except Exception as e:
                self.agent._hook_bus.emit("on_error", error=e)
                logger.error(f"Agent LLM chat error: {e}")
                yield ("error", {"error": str(e)})
                return

            if not response:
                yield ("error", {"error": "LLM returned empty response"})
                return

            # ---- Text-only = final reply ----
            if response.content and not response.has_tool_calls:
                last_text_stream = response.content.strip()
                yield ("text_stream", {"content": response.content})
                reply = response.content.strip() or "已完成。"
                for r in self.agent._hook_bus.emit("before_response", reply=reply, steps=all_steps_out):
                    if r is not None:
                        reply = r

                if session_id:
                    try:
                        await asyncio.to_thread(self.agent.db.add_agent_message, session_id, "assistant", reply, root_id=root_id)
                    except Exception as e:
                        logger.error(f"[Agent] save message error: {e}")

                payload: Dict[str, Any] = {
                    "ok": True,
                    "reply": reply,
                    "session_id": session_id,
                    "steps": all_steps_out,
                }
                yield ("done", payload)

                # 后处理（不阻塞用户回复）
                asyncio.create_task(
                    postprocess_turn(
                        self.agent, user_message, reply, all_steps_out,
                        step_counter, session_id,
                    )
                )
                return

            # ---- Tool calls ----
            if response.has_tool_calls:
                assistant_msg = {
                    "role": "assistant",
                    "content": response.content,
                    "reasoning_content": response.reasoning_content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in response.tool_calls
                    ],
                }
                messages.append(assistant_msg)

                if response.content:
                    try:
                        yield ("text_stream", {"content": response.content})
                    except Exception:
                        pass

                if session_id:
                    try:
                        await asyncio.to_thread(
                            self.agent.db.add_agent_message,
                            session_id, "assistant", json.dumps(assistant_msg, ensure_ascii=False),
                            reasoning_content=response.reasoning_content,
                            root_id=root_id,
                        )
                    except Exception as e:
                        logger.error(f"[Agent] save message error: {e}")

                for tc in response.tool_calls:
                    yield ("tool_call", {
                        "name": tc.name,
                        "preview": _arg_preview(tc.arguments),
                        "id": tc.id,
                    })

                    _blocked = False
                    for _r in self.agent._hook_bus.emit("before_tool_dispatch", name=tc.name, args=tc.arguments, ctx=tool_context):
                        if _r is False:
                            result = ToolResult(ok=False, error=f"工具 [{tc.name}] 被 hook 拦截", signal="__continue__")
                            _blocked = True
                            break
                        elif isinstance(_r, dict):
                            tc.arguments = _r
                    if not _blocked:
                        result = await asyncio.to_thread(self.agent.tool_registry.dispatch, tc.name, tc.arguments, tool_context)

                    for _r in self.agent._hook_bus.emit("after_tool_dispatch", name=tc.name, result=result, ctx=tool_context):
                        if _r is not None:
                            result = _r

                    for ev in _pending_events:
                        yield ev
                    _pending_events.clear()

                    yield ("tool_result", {
                        "name": tc.name,
                        "ok": result.ok,
                        "summary": result.content_text[:200] if result.ok else (result.error or "")[:200],
                    })

                    if result.signal == "__ask__":
                        return

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result.content_text[:3000],
                    })

                continue

            # ---- Empty response ----
            yield ("error", {"error": "LLM returned empty response"})
            return

        # ---- Loops exhausted ----
        payload: Dict[str, Any] = {
            "ok": True,
            "reply": final_reply or last_text_stream or "已执行完成。",
            "session_id": session_id,
            "steps": all_steps_out,
        }
        yield ("done", payload)


def _arg_preview(args: dict) -> str:
    """从工具参数中提取最重要的部分作为进度提示。"""
    if not args:
        return ""
    for key in ("query", "keyword", "question", "command", "path", "name", "skill", "package"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            v = val.strip()
            return v[:60] + ("..." if len(v) > 60 else "")
    for v in args.values():
        if isinstance(v, str) and v.strip():
            v = v.strip()
            return v[:60] + ("..." if len(v) > 60 else "")
    keys = list(args.keys())
    return ", ".join(keys[:3]) + ("..." if len(keys) > 3 else "")
