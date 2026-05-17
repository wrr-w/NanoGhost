import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    """A tool call from the LLM response."""
    id: str
    name: str
    arguments: Dict[str, Any]

    @classmethod
    def from_openai(cls, tc: Any) -> "ToolCall":
        return cls(
            id=tc.id,
            name=tc.function.name,
            arguments=json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments,
        )

    def to_message(self) -> Dict[str, Any]:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": self.id,
                    "type": "function",
                    "function": {
                        "name": self.name,
                        "arguments": json.dumps(self.arguments, ensure_ascii=False),
                    },
                }
            ],
        }


@dataclass
class ToolDefinition:
    """Definition/JSON schema for a tool, compatible with OpenAI format."""
    name: str
    description: str
    parameters: Dict[str, Any]
    category: str = "general"

    def to_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolResult:
    """Result from dispatching a tool call."""
    ok: bool
    data: Any = None
    error: Optional[str] = None
    signal: Optional[str] = None
    """Optional signal for the agent loop: '__done__', '__ask__', etc."""

    @property
    def content_text(self) -> str:
        if self.error:
            return f"错误: {self.error}"
        if isinstance(self.data, str):
            return self.data
        return json.dumps(self.data, ensure_ascii=False, indent=2)[:4000]
