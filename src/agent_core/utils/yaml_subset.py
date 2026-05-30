import os
import re
from pathlib import Path
from typing import Any, Dict, Union


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


def load_yaml_subset(path: Union[str, Path]) -> Dict[str, Any]:
    """Load a YAML file using the standard PyYAML parser.

    Returns an empty dict if the file doesn't exist or fails to parse.
    The result dict contains plain Python types (dict, list, str, int, float, bool, None).
    Callers apply substitute_env() on individual values where needed.
    """
    import yaml

    p = Path(path)
    if not p.exists():
        return {}

    raw = p.read_text(encoding="utf-8-sig")
    if not raw.strip():
        return {}

    try:
        result = yaml.safe_load(raw)
    except Exception:
        return {}

    if result is None:
        return {}
    if not isinstance(result, dict):
        return {}

    return result


