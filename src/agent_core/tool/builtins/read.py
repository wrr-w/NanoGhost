from typing import Any, Dict

from ..models import ToolResult


def read_file(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    """Read a file from the local filesystem."""
    path = (args.get("path") or "").strip()
    if not path:
        return ToolResult(ok=False, error="缺少 path 参数")
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        return ToolResult(ok=True, data=content)
    except FileNotFoundError:
        return ToolResult(ok=False, error=f"文件不存在: {path}")
    except Exception as e:
        return ToolResult(ok=False, error=f"读取失败: {e}")


READ_DEF = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "文件的绝对路径",
        },
    },
    "required": ["path"],
}
