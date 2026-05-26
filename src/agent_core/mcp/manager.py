import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent_core.tool import ToolRegistry, ToolResult

from .config import MCPServerConfig, load_global_registry, mask_headers, resolve_servers
from .http_sse import MCPHttpSSEClient
from .stdio_client import MCPStdioClient


_MCP_TOOL_PREFIX = "mcp."


@dataclass
class ServerCache:
    status: str = "disconnected"
    last_probe_at: float = 0.0
    last_error: str = ""
    cooldown_until: float = 0.0
    fail_count: int = 0
    tools: Dict[str, Dict[str, Any]] = None  # type: ignore
    tools_hash: str = ""


def _hash_tools(tools: Dict[str, Dict[str, Any]]) -> str:
    try:
        raw = json.dumps(tools, ensure_ascii=False, sort_keys=True)
    except Exception:
        raw = str(tools)
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _instance_dir_from_env() -> Optional[Path]:
    inst = (os.getenv("INSTANCE_DIR") or "").strip()
    if not inst:
        return None
    return Path(os.path.abspath(os.path.expanduser(inst)))


def _extract_tools(result_obj: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(result_obj, dict):
        return {}
    tools = result_obj.get("tools")
    if not isinstance(tools, list):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for t in tools:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        out[name] = t
    return out


def _mcp_tool_schema(server_id: str, tool_name: str, tool_obj: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    desc = str(tool_obj.get("description") or "").strip()
    desc = f"[MCP:{server_id}] {desc}".strip()

    inp = tool_obj.get("inputSchema")
    if isinstance(inp, dict):
        params = dict(inp)
        if params.get("type") is None:
            params["type"] = "object"
        if not isinstance(params.get("properties"), dict):
            params["properties"] = {}
        if not isinstance(params.get("required"), list):
            params.pop("required", None)
    else:
        params = {"type": "object", "properties": {}}
        if desc:
            desc = f"{desc} (schema incomplete)"
        else:
            desc = f"[MCP:{server_id}] (schema incomplete)"

    exposed = f"{_MCP_TOOL_PREFIX}{server_id}.{tool_name}"
    return exposed, desc, params


class MCPManager:
    def __init__(self, cooldown_seconds: int = 60, fail_threshold: int = 3,
                 probe_ttl_seconds: int = 60):
        self._lock = threading.Lock()
        self._registry: Optional[ToolRegistry] = None
        self._servers: Dict[str, MCPServerConfig] = {}
        self._clients: Dict[str, MCPHttpSSEClient] = {}
        self._cache: Dict[str, ServerCache] = {}
        self._last_loaded_instance: str = ""
        self._probe_ttl_seconds = probe_ttl_seconds
        self._fail_threshold = fail_threshold
        self._cooldown_seconds = cooldown_seconds
        self._poller_thread: Optional[threading.Thread] = None
        self._poller_stop = threading.Event()

    def attach_tool_registry(self, registry: ToolRegistry) -> None:
        with self._lock:
            self._registry = registry

    def _ensure_loaded(self, instance_dir: Path) -> None:
        inst_key = str(instance_dir)
        if inst_key == self._last_loaded_instance and self._servers:
            return
        servers = resolve_servers(instance_dir)
        self._servers = {s.server_id: s for s in servers}
        self._clients = {}
        for sid, cfg in self._servers.items():
            if cfg.transport == "stdio":
                self._clients[sid] = MCPStdioClient(cfg)
            else:
                self._clients[sid] = MCPHttpSSEClient(cfg)
        for sid in list(self._cache.keys()):
            if sid not in self._servers:
                self._cache.pop(sid, None)
        self._last_loaded_instance = inst_key

    def _unregister_server_tools(self, server_id: str) -> None:
        reg = self._registry
        if reg is None:
            return
        prefix = f"{_MCP_TOOL_PREFIX}{server_id}."
        for name in list(reg.list_tools()):
            if name.startswith(prefix):
                reg.unregister(name)

    def _register_server_tools(self, server_id: str, tools: Dict[str, Dict[str, Any]]) -> None:
        reg = self._registry
        if reg is None:
            return
        for tool_name, tool_obj in tools.items():
            exposed, desc, params = _mcp_tool_schema(server_id, tool_name, tool_obj)

            def _handler(args: Dict[str, Any], ctx: Dict[str, Any], _sid=server_id, _tn=tool_name) -> ToolResult:
                ok, data, err, dur = self.call_tool(_sid, _tn, args or {})
                payload = {
                    "ok": ok,
                    "data": data if ok else (data or {}),
                    "error": None if ok else (err or "mcp tool call failed"),
                    "meta": {
                        "server_id": _sid,
                        "tool_name": _tn,
                        "duration_ms": int(dur or 0),
                    },
                }
                return ToolResult(ok=ok, data=payload)

            reg.register(exposed, _handler, description=desc, parameters=params)

    def refresh_all(self, instance_dir: Optional[Path] = None) -> None:
        inst = instance_dir or _instance_dir_from_env()
        if inst is None:
            return

        import logging
        _log = logging.getLogger('agent_core')
        _log.info(f'[MCP] refresh_all: {inst}')

        with self._lock:
            self._ensure_loaded(inst)
            servers = list(self._servers.keys())


        for sid in servers:
            self.refresh_server(sid, instance_dir=inst)

    def refresh_server(self, server_id: str, instance_dir: Optional[Path] = None) -> None:
        inst = instance_dir or _instance_dir_from_env()
        if inst is None:
            return

        import logging
        _log = logging.getLogger('agent_core')

        with self._lock:
            self._ensure_loaded(inst)
            cfg = self._servers.get(server_id)
            client = self._clients.get(server_id)
            cache = self._cache.get(server_id) or ServerCache(tools={})
            self._cache[server_id] = cache

        if not cfg or not client:
            self._unregister_server_tools(server_id)
            return

        now = time.time()
        if cache.cooldown_until and now < cache.cooldown_until:
            self._unregister_server_tools(server_id)
            return

        if cache.last_probe_at and now - cache.last_probe_at < self._probe_ttl_seconds and cache.tools:
            return

        # stdio: probe is redundant — list_tools() internally calls _ensure_connected()
        _timer = time.time()
        try:
            from concurrent.futures import ThreadPoolExecutor, TimeoutError
            _exe = ThreadPoolExecutor(max_workers=1)
            try:
                _ft = _exe.submit(client.list_tools)
                ok, result, err, _dur = _ft.result(timeout=40)
            except TimeoutError:
                _ft.cancel()
                raise
            finally:
                _exe.shutdown(wait=False)
        except Exception as _e:
            ok, result, err, _dur = False, None, str(_e), int((time.time() - _timer)*1000)
            _log.error(f'[MCP] {server_id}: list_tools exception: {_e}')
        _log.info(f'[MCP] {server_id}: list_tools took {int((time.time()-_timer)*1000)}ms, ok={ok}')
        cache.last_probe_at = now
        if not ok:
            cache.status = "error"
            cache.last_error = err or "list_tools failed"
            cache.fail_count += 1
            _log.error(f'[MCP] {server_id}: list_tools failed: {err}')
            if cache.fail_count >= self._fail_threshold:
                cache.cooldown_until = now + self._cooldown_seconds
            self._unregister_server_tools(server_id)
            return

        tools = _extract_tools(result)
        tools_hash = _hash_tools(tools)
        if tools_hash != cache.tools_hash:
            self._unregister_server_tools(server_id)
            self._register_server_tools(server_id, tools)
            cache.tools = tools
            cache.tools_hash = tools_hash
        _log.info(f'[MCP] {server_id}: {len(tools)} tools registered')
        cache.status = "connected"
        cache.last_error = ""
        cache.fail_count = 0
        cache.cooldown_until = 0.0

    def call_tool(self, server_id: str, tool_name: str, arguments: Dict[str, Any]) -> Tuple[bool, Any, Optional[str], int]:
        inst = _instance_dir_from_env()
        if inst is None:
            return False, None, "INSTANCE_DIR not set", 0

        with self._lock:
            self._ensure_loaded(inst)
            client = self._clients.get(server_id)
            cache = self._cache.get(server_id) or ServerCache(tools={})
            self._cache[server_id] = cache

        now = time.time()
        if cache.cooldown_until and now < cache.cooldown_until:
            return False, None, "server in cooldown", 0

        if client is None:
            return False, None, "server not available", 0

        ok, result, err, dur = client.call_tool(tool_name, arguments or {})
        if ok:
            cache.status = "connected"
            cache.last_error = ""
            cache.fail_count = 0
            cache.cooldown_until = 0.0
            return True, result, None, dur

        cache.status = "error"
        cache.last_error = err or "call_tool failed"
        cache.fail_count += 1
        if cache.fail_count >= self._fail_threshold:
            cache.cooldown_until = now + self._cooldown_seconds
        return False, result, cache.last_error, dur

    def start_poller(self, interval_seconds: int = 60) -> None:
        if self._poller_thread is not None and self._poller_thread.is_alive():
            return
        self._poller_stop.clear()
        self._poller_thread = threading.Thread(target=self._poller_loop,
                                                args=(interval_seconds,), daemon=True)
        self._poller_thread.start()

    def stop_poller(self) -> None:
        self._poller_stop.set()
        if self._poller_thread:
            self._poller_thread.join(timeout=2)
            self._poller_thread = None

    def _poller_loop(self, interval_seconds: int) -> None:
        while not self._poller_stop.wait(timeout=interval_seconds):
            try:
                self.refresh_all()
            except Exception:
                pass

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            global_cfg = load_global_registry()
            reg = global_cfg.get("mcp_servers") if isinstance(global_cfg, dict) else {}
            reg = reg if isinstance(reg, dict) else {}
            servers = []
            for sid, cfg in self._servers.items():
                cache = self._cache.get(sid) or ServerCache(tools={})
                servers.append(
                    {
                        "server_id": sid,
                        "enabled": True,
                        "transport": cfg.transport,
                        "url": cfg.url,
                        "headers": mask_headers(cfg.headers),
                        "status": cache.status,
                        "last_error": cache.last_error,
                        "tools_count": len(cache.tools or {}),
                    }
                )
            return {"global": {"count": len(reg)}, "effective": servers}

    @property
    def registry(self) -> Optional[ToolRegistry]:
        with self._lock:
            return self._registry
