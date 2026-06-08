from typing import Any, Dict

from ..models import ToolResult


def ask_user(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """Ask the user a question and wait for response."""
    question = args.get("question") or "请确认"
    options = args.get("options") or []
    yield_event = ctx.get("yield_event")
    session_id = ctx.get("session_id")
    if yield_event:
        payload = {"question": question, "options": options, "session_id": session_id}
        yield_event("ask_user", payload)
    return ToolResult(ok=True, data=question, signal="__ask__")


ASK_USER_DEF = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "向用户提出的问题"},
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "可选答案列表",
        },
    },
    "required": ["question"],
}
