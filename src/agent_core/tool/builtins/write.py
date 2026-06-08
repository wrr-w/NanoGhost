import os
from typing import Any, Dict

from ..models import ToolResult


def write_file(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    file_path = (args.get("file_path") or args.get("path") or "").strip()
    content = args.get("content", "")
    if not file_path:
        return ToolResult(ok=False, error="缺少 file_path 参数")

    try:
        os.makedirs(os.path.dirname(os.path.abspath(file_path)) or ".", exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return ToolResult(ok=True, data=f"文件已写入: {file_path} ({len(content)} 字符)")
    except Exception as e:
        return ToolResult(ok=False, error=f"写入失败: {e}")


WRITE_DEF = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "文件的绝对路径",
        },
        "content": {
            "type": "string",
            "description": "要写入文件的完整内容",
        },
    },
    "required": ["file_path", "content"],
}
