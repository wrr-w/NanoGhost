import json
import re
from typing import Any, Dict, Union


def extract_json_from_llm_response(text: str) -> Union[Dict[str, Any], list, None]:
    """从 LLM 回复中提取 JSON 对象（容忍 markdown 代码块包裹和非严格 JSON）。"""
    text = (text or "").strip()
    if not text:
        return None

    # 尝试直接 parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # 尝试 json5（容忍 trailing comma、注释等）
    try:
        import json5
        return json5.loads(text)
    except Exception:
        pass

    # 提取 ```json ... ``` 或 ``` ... ```
    for pat in (
        r"```(?:json|JSON)\s*\n?(.*?)\n?```",
        r"```\s*\n?(.*?)\n?```",
        r"`(.*?)`",
    ):
        m = re.search(pat, text, re.DOTALL)
        if m:
            candidate = m.group(1).strip()
            try:
                return json.loads(candidate)
            except Exception:
                try:
                    import json5
                    return json5.loads(candidate)
                except Exception:
                    continue

    # 最后一搏：查找第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start:end+1]
        try:
            return json.loads(candidate)
        except Exception:
            try:
                import json5
                return json5.loads(candidate)
            except Exception:
                pass

    return None
