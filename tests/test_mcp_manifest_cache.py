import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for candidate in (str(ROOT), str(SRC)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from agent_core.mcp_client.manifest_cache import load_manifest_cache, save_manifest_cache


def test_manifest_cache_roundtrip(tmp_path: Path):
    save_manifest_cache(
        tmp_path,
        "capture",
        {
            "server_id": "capture",
            "title": "Capture",
            "actions": [{"name": "capture_api_call", "description": "generic fallback"}],
            "last_manifest_refresh_at": "2026-07-06T12:00:00Z",
        },
    )

    payload = load_manifest_cache(tmp_path, "capture")

    assert payload is not None
    assert payload["server_id"] == "capture"
    assert payload["actions"][0]["name"] == "capture_api_call"


def test_manifest_cache_missing_returns_none(tmp_path: Path):
    assert load_manifest_cache(tmp_path, "iwms") is None
