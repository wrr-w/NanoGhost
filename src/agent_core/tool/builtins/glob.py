import fnmatch
import os
from typing import Any, Dict, List

from ..models import ToolResult


def glob_files(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    pattern = (args.get("pattern") or "").strip()
    path = (args.get("path") or ".").strip()

    if not pattern:
        return ToolResult(ok=False, error="缺少 pattern 参数")

    try:
        if not os.path.isdir(path):
            return ToolResult(ok=False, error=f"目录不存在: {path}")

        matches: List[str] = []
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, path)
                if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
                    matches.append(rel.replace("\\", "/"))
                    if len(matches) >= 200:
                        break
            if len(matches) >= 200:
                break

        if not matches:
            return ToolResult(ok=True, data="(无匹配文件)")

        matches.sort()
        result = f"匹配到 {len(matches)} 个文件:\n" + "\n".join(f"  {m}" for m in matches[:100])
        if len(matches) > 100:
            result += f"\n  ... 还有 {len(matches) - 100} 个"
        return ToolResult(ok=True, data=result)
    except Exception as e:
        return ToolResult(ok=False, error=f"glob 失败: {e}")


GLOB_DEF = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "glob 文件名模式，如 **/*.py 或 src/*.ts",
        },
        "path": {
            "type": "string",
            "description": "搜索起始目录（默认当前目录）",
        },
    },
    "required": ["pattern"],
}
