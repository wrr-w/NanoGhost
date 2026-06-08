import os
from typing import Any, Dict

from ..models import ToolResult


def edit_file(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    file_path = (args.get("file_path") or args.get("path") or "").strip()
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    replace_all = args.get("replace_all", False)

    if not file_path:
        return ToolResult(ok=False, error="缺少 file_path 参数")
    if not old_string and old_string != "":
        return ToolResult(ok=False, error="缺少 old_string 参数")

    try:
        if not os.path.isfile(file_path):
            return ToolResult(ok=False, error=f"文件不存在: {file_path}")

        with open(file_path, "r", encoding="utf-8") as f:
            original = f.read()

        count = original.count(old_string)
        if count == 0:
            return ToolResult(ok=False, error="old_string 在文件中未找到")
        if count > 1 and not replace_all:
            return ToolResult(
                ok=False,
                error=f"old_string 匹配了 {count} 处。请提供更多上下文使其唯一，或设置 replace_all=true。",
            )

        modified = original.replace(old_string, new_string)
        if modified == original:
            return ToolResult(ok=False, error="替换后内容无变化")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(modified)

        replaced = count if replace_all else 1
        return ToolResult(ok=True, data=f"文件已编辑: {file_path} (替换了 {replaced} 处)")
    except Exception as e:
        return ToolResult(ok=False, error=f"编辑失败: {e}")


EDIT_DEF = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "要编辑的文件的绝对路径",
        },
        "old_string": {
            "type": "string",
            "description": "要替换的文本，必须与文件中的内容精确匹配",
        },
        "new_string": {
            "type": "string",
            "description": "替换后的文本",
        },
        "replace_all": {
            "type": "boolean",
            "description": "是否替换所有出现的 old_string（默认 false，必须唯一匹配）",
        },
    },
    "required": ["file_path", "old_string", "new_string"],
}
