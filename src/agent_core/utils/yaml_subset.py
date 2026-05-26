import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union


_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def substitute_env(value: str) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    if len(s) >= 2 and ((s[0] == s[-1] == "`") or (s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        s = s[1:-1].strip()

    def _repl(m: re.Match) -> str:
        key = m.group(1)
        return os.getenv(key) or ""

    return _ENV_RE.sub(_repl, s)


def _parse_scalar(raw: str) -> Any:
    s = raw.strip()
    if not s:
        return ""
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s in ("null", "None", "~"):
        return None
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")) or (s.startswith("`") and s.endswith("`")):
        return s[1:-1]
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except Exception:
            return s
    if re.fullmatch(r"-?\d+\.\d+", s):
        try:
            return float(s)
        except Exception:
            return s
    return s


def load_yaml_subset(path: Union[str, Path]) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    root: Dict[str, Any] = {}

    stack: List[Tuple[int, Any]] = [(0, root)]
    last_key_stack: List[str] = [""]

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if not line.strip():
            continue
        stripped = line.lstrip(" ")
        if stripped.startswith("#"):
            continue

        indent = len(line) - len(stripped)
        while len(stack) > 1 and indent < stack[-1][0]:
            stack.pop()
            last_key_stack.pop()

        parent = stack[-1][1]

        if stripped.startswith("- "):
            item_raw = stripped[2:].strip()
            if not isinstance(parent, list):
                if isinstance(parent, dict):
                    last_key = last_key_stack[-1]
                    if last_key and isinstance(parent.get(last_key), list):
                        parent = parent[last_key]
                        stack[-1] = (stack[-1][0], parent)
                    else:
                        outer = stack[-2][1]
                        outer[last_key] = []
                        parent = outer[last_key]
                        stack[-1] = (stack[-1][0], parent)
                else:
                    continue

            if ":" in item_raw:
                is_win_path = len(item_raw) > 2 and item_raw[1] == ":" and item_raw[2] in ("\\", "/")
                if is_win_path:
                    parent.append(_parse_scalar(item_raw))
                    continue
                k, _, v = item_raw.partition(":")
                k = k.strip()
                v = v.strip()
                item: Dict[str, Any] = {}
                if v:
                    item[k] = _parse_scalar(v)
                    parent.append(item)
                else:
                    item[k] = {}
                    parent.append(item)
                    stack.append((indent + 2, item[k]))
                    last_key_stack.append(k)
            else:
                parent.append(_parse_scalar(item_raw))
            continue

        if ":" not in stripped:
            continue

        key, _, value_raw = stripped.partition(":")
        key = key.strip()
        value_raw = value_raw.strip()

        if isinstance(parent, list):
            if not parent:
                parent.append({})
            if not isinstance(parent[-1], dict):
                parent.append({})
            parent = parent[-1]

        if value_raw:
            parent[key] = _parse_scalar(value_raw)
            last_key_stack[-1] = key
        else:
            parent[key] = {}
            stack.append((indent + 2, parent[key]))
            last_key_stack.append(key)

    return root
