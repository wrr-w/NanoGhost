import os
import re
from typing import Any, Dict, List

from ..models import ToolResult


_MAX_MATCHES = 100
_MAX_LINE_LEN = 200


def grep_search(args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    pattern = (args.get("pattern") or "").strip()
    path = (args.get("path") or ".").strip()
    include = (args.get("include") or "").strip()

    if not pattern:
        return ToolResult(ok=False, error="缺少 pattern 参数")

    try:
        re.compile(pattern)
    except re.error as e:
        return ToolResult(ok=False, error=f"无效的正则表达式: {e}")

    try:
        if not os.path.isdir(path):
            return ToolResult(ok=False, error=f"目录不存在: {path}")

        include_globs = [g.strip() for g in include.split(",") if g.strip()] if include else None

        results: List[str] = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git")]
            for filename in files:
                if include_globs:
                    if not any(fnmatch(filename, g) for g in include_globs):
                        continue
                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        for lineno, line in enumerate(f, 1):
                            if re.search(pattern, line):
                                rel = os.path.relpath(filepath, path).replace("\\", "/")
                                line_stripped = line.rstrip("\n\r")
                                if len(line_stripped) > _MAX_LINE_LEN:
                                    line_stripped = line_stripped[:_MAX_LINE_LEN] + "..."
                                results.append(f"{rel}:{lineno}: {line_stripped}")
                                if len(results) >= _MAX_MATCHES:
                                    break
                except (OSError, UnicodeDecodeError):
                    continue
                if len(results) >= _MAX_MATCHES:
                    break
            if len(results) >= _MAX_MATCHES:
                break

        if not results:
            return ToolResult(ok=True, data="(无匹配)")

        out = f"匹配到 {len(results)} 行:\n" + "\n".join(results[:_MAX_MATCHES])
        if len(results) > _MAX_MATCHES:
            out += f"\n  ... 还有 {len(results) - _MAX_MATCHES} 行"
        return ToolResult(ok=True, data=out)
    except Exception as e:
        return ToolResult(ok=False, error=f"grep 失败: {e}")


def fnmatch(name: str, pattern: str) -> bool:
    import fnmatch as _fnmatch
    return _fnmatch.fnmatch(name, pattern)


GREP_DEF = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "正则表达式搜索模式",
        },
        "path": {
            "type": "string",
            "description": "搜索目录路径（默认当前目录）",
        },
        "include": {
            "type": "string",
            "description": "逗号分隔的文件名 glob，如 *.py,*.ts（可选）",
        },
    },
    "required": ["pattern"],
}
