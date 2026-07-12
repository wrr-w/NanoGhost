import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for candidate in (str(ROOT), str(SRC)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from agent_core.mcp_client.config import resolve_servers
from agent_core.mcp_client.manifest_cache import save_manifest_cache
from agent_core.cli import _cmd_mcp_manifest
from agent_core.mcp_client.manager import MCPManager, ServerCache


def test_resolve_servers_parses_optional_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NANOGHOST_GLOBAL_CONFIG", str(tmp_path / "global.yaml"))
    (tmp_path / "global.yaml").write_text(
        "mcp_servers:\n"
        "  capture:\n"
        "    transport: stdio\n"
        "    command: python\n"
        "    args: ['capture.py']\n"
        "    title: Capture\n"
        "    description: Capture task system\n"
        "    use_cases: ['screenshots', 'exports']\n"
        "    notes: Prefer when collecting task evidence.\n",
        encoding="utf-8",
    )
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / "config.yaml").write_text("mcp:\n  enabled_only:\n    - capture\n", encoding="utf-8")

    servers = resolve_servers(inst)

    assert len(servers) == 1
    assert servers[0].server_id == "capture"
    assert servers[0].title == "Capture"
    assert servers[0].description == "Capture task system"
    assert servers[0].use_cases == ["screenshots", "exports"]
    assert servers[0].notes == "Prefer when collecting task evidence."


def test_build_awareness_summary_includes_enabled_server_description(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NANOGHOST_GLOBAL_CONFIG", str(tmp_path / "global.yaml"))
    (tmp_path / "global.yaml").write_text(
        "mcp_servers:\n"
        "  capture:\n"
        "    transport: stdio\n"
        "    command: python\n"
        "    args: ['capture.py']\n"
        "    description: Capture task system\n",
        encoding="utf-8",
    )
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / "config.yaml").write_text("mcp:\n  enabled_only:\n    - capture\n", encoding="utf-8")

    mgr = MCPManager()
    summary = mgr.build_awareness_summary(inst)

    assert summary is not None
    assert "capture" in summary
    assert "Capture task system" in summary
    assert "status=known" in summary


def test_build_awareness_summary_reflects_runtime_statuses(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NANOGHOST_GLOBAL_CONFIG", str(tmp_path / "global.yaml"))
    (tmp_path / "global.yaml").write_text(
        "mcp_servers:\n"
        "  capture:\n"
        "    transport: stdio\n"
        "    command: python\n"
        "    args: ['capture.py']\n"
        "    description: Capture task system\n"
        "    use_cases: ['screenshots', 'exports']\n"
        "  broken:\n"
        "    transport: stdio\n"
        "    command: python\n"
        "    args: ['broken.py']\n"
        "    description: Broken service\n",
        encoding="utf-8",
    )
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / "config.yaml").write_text(
        "mcp:\n"
        "  enabled_only:\n"
        "    - capture\n"
        "    - broken\n",
        encoding="utf-8",
    )

    mgr = MCPManager()
    mgr._ensure_loaded(inst)
    mgr._cache["capture"] = ServerCache(status="ready", tools={"snap": {"name": "snap"}})
    mgr._cache["broken"] = ServerCache(status="error", last_error="boom", tools={})

    summary = mgr.build_awareness_summary(inst)

    assert summary is not None
    assert "status=ready" in summary
    assert "status=error" in summary
    assert "screenshots, exports" in summary


def test_build_awareness_summary_uses_manifest_when_server_is_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NANOGHOST_GLOBAL_CONFIG", str(tmp_path / "global.yaml"))
    (tmp_path / "global.yaml").write_text(
        "mcp_servers:\n"
        "  capture:\n"
        "    transport: sse\n"
        "    url: http://127.0.0.1:8000/mcp/sse\n",
        encoding="utf-8",
    )
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / "config.yaml").write_text("mcp:\n  enabled_only:\n    - capture\n", encoding="utf-8")
    save_manifest_cache(
        inst,
        "capture",
        {
            "server_id": "capture",
            "title": "Capture",
            "description": "Capture service MCP",
            "actions": [{"name": "capture_api_call", "description": "generic fallback"}],
        },
    )

    mgr = MCPManager()
    mgr._ensure_loaded(inst)
    mgr._cache["capture"] = ServerCache(status="error", last_error="connection refused", tools={})

    summary = mgr.build_awareness_summary(inst)

    assert summary is not None
    assert "Capture service MCP" in summary
    assert "capture_api_call" in summary
    assert "status=error" in summary
    assert "connection refused" in summary


def test_cmd_mcp_manifest_reports_cached_actions(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NANOGHOST_GLOBAL_CONFIG", str(tmp_path / "global.yaml"))
    (tmp_path / "global.yaml").write_text(
        "mcp_servers:\n"
        "  capture:\n"
        "    transport: sse\n"
        "    url: http://127.0.0.1:8000/mcp/sse\n",
        encoding="utf-8",
    )
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / "config.yaml").write_text("mcp:\n  enabled_only:\n    - capture\n", encoding="utf-8")
    save_manifest_cache(
        inst,
        "capture",
        {
            "server_id": "capture",
            "title": "Capture",
            "description": "Capture service MCP",
            "actions": [{"name": "capture_api_call", "description": "generic fallback"}],
            "last_manifest_refresh_at": "2026-07-06T12:00:00Z",
        },
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cmd_mcp_manifest(SimpleNamespace(instance_dir=str(inst)))

    payload = json.loads(buf.getvalue())
    assert rc == 0
    assert payload["results"][0]["server_id"] == "capture"
    assert payload["results"][0]["has_manifest"] is True
    assert payload["results"][0]["actions_count"] == 1
