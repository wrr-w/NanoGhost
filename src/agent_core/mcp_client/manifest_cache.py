import json
from pathlib import Path
from typing import Any, Dict, Optional


def _manifest_dir(instance_dir: Path) -> Path:
    return instance_dir / ".mcp"


def _manifest_path(instance_dir: Path, server_id: str) -> Path:
    return _manifest_dir(instance_dir) / f"{server_id}.manifest.json"


def load_manifest_cache(instance_dir: Path, server_id: str) -> Optional[Dict[str, Any]]:
    path = _manifest_path(instance_dir, server_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def save_manifest_cache(instance_dir: Path, server_id: str, payload: Dict[str, Any]) -> Path:
    root = _manifest_dir(instance_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = _manifest_path(instance_dir, server_id)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
