from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Iterator, Optional


@dataclass
class LLMResponse:
    """LLM chat response that may contain text content and/or tool calls."""
    content: Optional[str] = None
    tool_calls: Optional[List[Any]] = None
    """List of ToolCall objects (from agent_core.tool.models) or None."""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMPort(ABC):
    """LLM 端口：流式对话 + embedding。"""

    @abstractmethod
    def stream_chat(
        self, messages: List[Dict], temperature: float = 0.1,
    ) -> Iterator[str]: ...

    @abstractmethod
    def embed(self, text: str) -> List[float]: ...

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.1,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        """Non-streaming chat with optional tool support.

        Default implementation: accumulate stream_chat text and try to
        parse JSON for backward compatibility. Subclasses (e.g. OpenAILLM)
        should override to support native tool calling.

        Args:
            messages: Chat messages (OpenAI format).
            temperature: Sampling temperature.
            tools: Optional list of OpenAI-format tool schemas.

        Returns:
            LLMResponse with content and/or tool_calls.
        """
        full_content = ""
        for chunk in self.stream_chat(messages, temperature=temperature):
            full_content += chunk
        return LLMResponse(content=full_content)
