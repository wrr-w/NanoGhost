import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent_core.tool import ToolRegistry, ToolResult

from .config import MCPServerConfig, load_global_registry, mask_headers, resolve_servers
from .http_sse import MCPHttpSSEClient
from .manifest_cache import load_manifest_cache, save_manifest_cache
from .stdio_client import MCPStdioClient


_MCP_TOOL_PREFIX = "mcp_"


@dataclass
class ServerCache:
    status: str = "known"
    last_probe_at: float = 0.0
    last_error: str = ""
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


def _manifest_payload_from_tools(cfg: MCPServerConfig, tools: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    refreshed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "server_id": cfg.server_id,
        "title": cfg.title or cfg.server_id,
        "description": cfg.description or "",
        "transport": cfg.transport,
        "endpoint": cfg.url,
        "actions": [
            {
                "name": name,
                "description": str(tool.get("description") or ""),
                "inputSchema": tool.get("inputSchema") or {},
            }
            for name, tool in sorted(tools.items())
        ],
        "last_manifest_refresh_at": refreshed_at,
    }


def _normalize_status(raw_status: str, tools: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    status = (raw_status or "").strip().lower()
    if status in {"known", "loading", "ready", "stale", "error"}:
        return status
    if status == "connected":
        return "ready" if tools else "loading"
    if status in {"disconnected", ""}:
        return "known"
    return status


def _build_server_tool_schema(server_id: str, tools: Dict[str, Dict[str, Any]]) -> Tuple[str, str, Dict[str, Any]]:
    """为整个 MCP 服务器生成一个组合工具 schema。

    将所有工具折叠为一个工具，通过 action 参数路由。
    """
    exposed = f"{_MCP_TOOL_PREFIX}{server_id}"

    # 构建 action enum 和描述
    actions = []
    property_schemas = {}
    for tname, tobj in tools.items():
        desc = str(tobj.get("description") or "").strip()
        actions.append({"name": tname, "description": desc})

        # 收集该工具的 inputSchema 作为说明
        inp = tobj.get("inputSchema")
        if isinstance(inp, dict):
            property_schemas[tname] = {
                "description": desc,
                "inputSchema": inp,
            }

    action_names = [a["name"] for a in actions]
    action_desc = "; ".join(f"{a['name']}: {a['description']}" for a in actions[:5])
    if len(actions) > 5:
        action_desc += f"; ... 共 {len(actions)} 个操作"

    params = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": action_names,
                "description": f"要执行的操作。可选: {', '.join(action_names)}",
            },
            "params": {
                "type": "object",
                "description": f"操作参数，具体字段取决于 action 的选择。{action_desc}",
            },
        },
        "required": ["action"],
    }

    desc = f"[MCP:{server_id}] 调用 {server_id} 服务器的 MCP 工具。通过 action 选择具体操作，params 传入对应参数。"
    return exposed, desc, params


class MCPManager:
    def __init__(self, fail_threshold: int = 3,
                 probe_ttl_seconds: int = 60):
        self._lock = threading.Lock()
        self._registry: Optional[ToolRegistry] = None
        self._servers: Dict[str, MCPServerConfig] = {}
        self._clients: Dict[str, MCPHttpSSEClient] = {}
        self._cache: Dict[str, ServerCache] = {}
        self._last_loaded_instance: str = ""
        self._probe_ttl_seconds = probe_ttl_seconds
        self._fail_threshold = fail_threshold
        self._poller_thread: Optional[threading.Thread] = None
        self._poller_stop = threading.Event()

    def attach_tool_registry(self, registry: ToolRegistry) -> None:
        with self._lock:
            self._registry = registry

    def _ensure_loaded(self, instance_dir: Path, force: bool = False) -> None:
        inst_key = str(instance_dir)
        if not force and inst_key == self._last_loaded_instance and self._servers:
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
        exposed = f"{_MCP_TOOL_PREFIX}{server_id}"
        reg.unregister(exposed)

    def _register_server_tools(self, server_id: str, tools: Dict[str, Dict[str, Any]]) -> None:
        reg = self._registry
        if reg is None:
            return
        
        # 读取 action 级白名单，过滤 tools
        inst = _instance_dir_from_env()
        if inst is not None:
            cfg_path = inst / "config.yaml"
            try:
                with open(str(cfg_path), encoding="utf-8") as f:
                    import yaml
                    cfg = yaml.safe_load(f) or {}
                mcp_cfg = cfg.get("mcp") or {}
                action_allowlist = mcp_cfg.get("action_allowlist") or {}
                allowed = action_allowlist.get(server_id)
                if allowed is not None and isinstance(allowed, list):
                    filtered = {}
                    for aname in allowed:
                        if aname in tools:
                            filtered[aname] = tools[aname]
                    if filtered:
                        tools = filtered
                    else:
                        # 白名单不为空但全被过滤掉了 => 该服务器无可用工具
                        return
            except Exception:
                pass
        
        exposed, desc, params = _build_server_tool_schema(server_id, tools)

        def _handler(args: Dict[str, Any], ctx: Dict[str, Any], _sid=server_id) -> ToolResult:
            action = (args.get("action") or "").strip()
            tool_params = args.get("params") or {}
            if not action:
                return ToolResult(ok=False, error=f"缺少 action 参数，可选: {list(tools.keys())}")
            if action not in tools:
                return ToolResult(ok=False, error=f"未知 action: {action}，可选: {list(tools.keys())}")
            ok, data, err, dur = self.call_tool(_sid, action, tool_params)
            payload = {
                "ok": ok,
                "data": data if ok else (data or {}),
                "error": None if ok else (err or "mcp tool call failed"),
                "meta": {
                    "server_id": _sid,
                    "tool_name": action,
                    "duration_ms": int(dur or 0),
                },
            }
            return ToolResult(ok=ok, data=payload)

        reg.register(exposed, _handler, description=desc, parameters=params, category="mcp")

    def refresh_all(self, instance_dir: Optional[Path] = None) -> None:
        inst = instance_dir or _instance_dir_from_env()
        if inst is None:
            return

        import logging
        _log = logging.getLogger('agent_core')
        _log.info(f'[MCP] refresh_all: {inst}')

        with self._lock:
            self._ensure_loaded(inst, force=True)
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
        # cooldown removed

        if cache.last_probe_at and now - cache.last_probe_at < self._probe_ttl_seconds and cache.tools:
            return

        cache.status = "loading"

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
            manifest = load_manifest_cache(inst, server_id)
            cache.status = "stale" if manifest else "error"
            cache.last_error = err or "list_tools failed"
            cache.fail_count += 1
            _log.error(f'[MCP] {server_id}: list_tools failed: {err}')
            # cooldown removed
            self._unregister_server_tools(server_id)
            return

        tools = _extract_tools(result)
        manifest_payload = _manifest_payload_from_tools(cfg, tools)
        save_manifest_cache(inst, server_id, manifest_payload)
        tools_hash = _hash_tools(tools)
        if tools_hash != cache.tools_hash:
            self._unregister_server_tools(server_id)
            self._register_server_tools(server_id, tools)
            cache.tools = tools
            cache.tools_hash = tools_hash
        _log.info(f'[MCP] {server_id}: {len(tools)} tools registered')
        cache.status = "ready"
        cache.last_error = ""
        cache.fail_count = 0

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
        # cooldown removed

        if client is None:
            return False, None, "server not available", 0

        ok, result, err, dur = client.call_tool(tool_name, arguments or {})
        if ok:
            cache.status = "ready"
            cache.last_error = ""
            cache.fail_count = 0
            cache.cooldown_until = 0.0
            return True, result, None, dur

        cache.status = "error"
        cache.last_error = err or "call_tool failed"
        cache.fail_count += 1
        # cooldown removed
        return False, result, cache.last_error, dur

    def start_poller(self, interval_seconds: int = 86400) -> None:
        # 环境变量 MCP_REFRESH_INTERVAL 可覆盖（单位秒）
        import os as _mcp_os
        interval_seconds = int(_mcp_os.environ.get("MCP_REFRESH_INTERVAL", str(interval_seconds)))
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

    def build_awareness_summary(self, instance_dir: Optional[Path] = None) -> Optional[str]:
        inst = instance_dir or _instance_dir_from_env()
        if inst is None:
            return None

        with self._lock:
            self._ensure_loaded(inst)
            servers = list(self._servers.values())
            cache_map = dict(self._cache)

        if not servers:
            return None

        lines: List[str] = ["## MCP 能力概览", ""]
        for cfg in servers:
            cache = cache_map.get(cfg.server_id) or ServerCache(tools={})
            manifest = load_manifest_cache(inst, cfg.server_id) or {}
            status = _normalize_status(cache.status, cache.tools)

            desc = cfg.description or str(manifest.get("description") or "").strip() or "已启用 MCP 服务"
            label = f"{cfg.server_id}"
            if cfg.title and cfg.title != cfg.server_id:
                label = f"{cfg.server_id} / {cfg.title}"
            lines.append(f"- `{label}` ({cfg.transport}): {desc} [status={status}]")
            if cfg.use_cases:
                lines.append(f"适用: {', '.join(cfg.use_cases[:3])}")
            actions = manifest.get("actions") if isinstance(manifest, dict) else None
            if isinstance(actions, list) and actions:
                preview = ", ".join(
                    str(item.get("name") or "").strip()
                    for item in actions[:5]
                    if isinstance(item, dict) and str(item.get("name") or "").strip()
                )
                if preview:
                    lines.append(f"操作: {preview}")
            if status in {"error", "stale"} and cache.last_error:
                lines.append(f"错误: {cache.last_error}")

        return "\n".join(lines)

    @property
    def registry(self) -> Optional[ToolRegistry]:
        with self._lock:
            return self._registry
