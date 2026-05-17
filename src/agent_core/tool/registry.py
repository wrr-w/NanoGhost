import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from .models import ToolDefinition, ToolResult

logger = logging.getLogger("agent_core")

ToolHandler = Callable[[Dict[str, Any], Dict[str, Any]], ToolResult]
"""handler(args, context) -> ToolResult"""

CheckFn = Callable[[], bool]
"""check_fn() -> True if tool should be exposed to the LLM"""


class ToolRegistry:
    """Registry for tool definitions and handlers.

    Tools are registered with a name, description, JSON schema for parameters,
    and a handler callable. The registry can export OpenAI-compatible schemas
    and dispatch incoming tool calls to the appropriate handler.
    """

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._handlers: Dict[str, ToolHandler] = {}
        self._check_fns: Dict[str, CheckFn] = {}

    def register(
        self,
        name: str,
        handler: ToolHandler,
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        category: str = "general",
        check_fn: Optional[CheckFn] = None,
    ) -> None:
        """Register a tool.

        Args:
            name: Tool name (must be unique).
            handler: Callable receiving (args, context) -> ToolResult.
            description: Description for the LLM.
            parameters: JSON Schema dict describing valid arguments.
            category: Tool category (system/skill/subagent).
            check_fn: Optional probe; if set and returns False, tool is hidden from LLM.
        """
        if name in self._tools:
            logger.warning(f"[ToolRegistry] Overwriting existing tool: {name}")
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            category=category,
        )
        self._handlers[name] = handler
        if check_fn:
            self._check_fns[name] = check_fn

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._handlers.pop(name, None)
        self._check_fns.pop(name, None)

    def get_definition(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def get_openai_schemas(self) -> List[Dict[str, Any]]:
        """Return ALL tool schemas (no filtering)."""
        return [t.to_openai_schema() for t in self._tools.values()]

    def get_available_schemas(self) -> List[Dict[str, Any]]:
        """Return schemas for tools whose check_fn passes (no check_fn = always available)."""
        result = []
        for name, td in self._tools.items():
            fn = self._check_fns.get(name)
            if fn is None or fn():
                result.append(td.to_openai_schema())
        return result

    def has_tools(self) -> bool:
        return len(self._tools) > 0

    def list_tools(self) -> List[str]:
        return list(self._tools.keys())

    def list_tools_by_category(self) -> Dict[str, List[str]]:
        """Return tools grouped by category."""
        groups: Dict[str, List[str]] = {}
        for name, td in self._tools.items():
            groups.setdefault(td.category, []).append(name)
        return groups

    def dispatch(self, name: str, args: Dict[str, Any], context: Dict[str, Any]) -> ToolResult:
        """Dispatch a tool call to its handler.

        Args:
            name: Tool name.
            args: Tool arguments (parsed JSON).
            context: Execution context dict (db, llm, http, config, etc.).

        Returns:
            ToolResult from the handler.

        Raises:
            KeyError if tool is not found.
        """
        handler = self._handlers.get(name)
        if handler is None:
            logger.error(f"[ToolRegistry] Unknown tool: {name}")
            return ToolResult(ok=False, error=f"未知工具: {name}")
        try:
            return handler(args, context)
        except Exception as e:
            logger.exception(f"[ToolRegistry] Handler error for {name}: {e}")
            return ToolResult(ok=False, error=str(e))
