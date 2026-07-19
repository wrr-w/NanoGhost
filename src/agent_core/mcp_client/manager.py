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


_MCP_TOOL_PREFIX = "mcp_"  # reserved, no longer auto-registered

def _path_join(parts: List[str]) -> str:
    return "/" + "/".join(parts)


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
        if registry is not None:
            self._register_meta_tools(registry)

    def _register_meta_tools(self, registry: ToolRegistry) -> None:
        """注册 MCP 渐进披露树形导航工具（全局一次）。"""
        if getattr(self, '_meta_registered', False):
            return
        self._meta_registered = True

        registry.register(
            "explore_mcp",
            self._handle_explore_mcp,
            description="树形导航 MCP 能力。path 类似文件系统路径：'/' 根节点列出所有服务器，'/server_id' 进入服务器查看 action，'/server_id/action' 为叶子节点显示完整参数 Schema。叶子节点带 params 参数可直接执行。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "树中路径，默认 '/'。'/' 返回所有服务器；'/capture' 返回 capture 下的操作；'/capture/status' 为叶子节点，显示参数 Schema，带 params 则执行。",
                    },
                    "params": {
                        "type": "object",
                        "description": "仅叶子节点使用。执行该 action 的参数，按返回的 inputSchema 构造 JSON。不带 params 时只返回 Schema。",
                    },
                },
                "required": ["path"],
            },
            category="system",
        )

    def _handle_explore_mcp(self, args: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
        raw_path = (args.get("path") or "/").strip()
        exec_params = args.get("params")
        if not raw_path.startswith("/"):
            raw_path = "/" + raw_path

        with self._lock:
            servers = dict(self._servers)
            cache_map = dict(self._cache)

        # ---- 根节点: 列出所有服务器 ----
        if raw_path == "/":
            items = []
            for sid, cfg in sorted(servers.items()):
                cache = cache_map.get(sid) or ServerCache(tools={})
                status = _normalize_status(cache.status, cache.tools)
                items.append({
                    "node_type": "server",
                    "name": sid,
                    "title": cfg.title or sid,
                    "description": cfg.description or "",
                    "status": status,
                    "actions_count": len(cache.tools or {}),
                })
            return ToolResult(ok=True, data={
                "path": "/",
                "node_type": "root",
                "children": items,
                "hint": "用 explore_mcp(path='/server_id') 进入某个服务器查看其 action。",
            })

        # ---- 中间/叶子节点: /server_id 或 /server_id/action ----
        parts = [p for p in raw_path.split("/") if p]
        if len(parts) < 1:
            return ToolResult(ok=False, error=f"无效路径: {raw_path}")

        server_id = parts[0]
        if server_id not in servers:
            return ToolResult(ok=False, error=f"未知服务器: {server_id}，可用: {list(servers.keys())}")

        cache = cache_map.get(server_id) or ServerCache(tools={})
        tools_dict = cache.tools or {}

        # ---- 中间节点: /server_id ----
        if len(parts) == 1:
            items = []
            for tname, tobj in sorted(tools_dict.items()):
                desc = str(tobj.get("description") or "").strip()
                items.append({
                    "node_type": "action",
                    "name": tname,
                    "description": desc[:200],
                })
            cfg = servers[server_id]
            return ToolResult(ok=True, data={
                "path": raw_path,
                "node_type": "server",
                "server_id": server_id,
                "title": cfg.title or server_id,
                "description": cfg.description or "",
                "children": items,
                "hint": "用 explore_mcp(path='/server_id/action_name') 查看 action 的完整参数。带 params 可直接执行。",
            })

        # ---- 叶子节点: /server_id/action ----
        action_name = parts[1]
        tobj = tools_dict.get(action_name)
        if not tobj:
            return ToolResult(ok=False, error=f"在服务器 {server_id} 上未找到 action: {action_name}，可用: {list(tools_dict.keys())}")

        input_schema = tobj.get("inputSchema") or {}
        desc = str(tobj.get("description") or "").strip()

        # 带 params → 直接执行
        if exec_params is not None:
            ok, data, err, dur = self.call_tool(server_id, action_name, exec_params)
            payload = {
                "ok": ok,
                "data": data if ok else (data or {}),
                "error": None if ok else (err or "mcp tool call failed"),
                "meta": {
                    "server_id": server_id,
                    "tool_name": action_name,
                    "duration_ms": int(dur or 0),
                },
            }
            return ToolResult(ok=ok, data=payload)

        # 不带 params → 只返回 Schema
        return ToolResult(ok=True, data={
            "path": raw_path,
            "node_type": "action",
            "server_id": server_id,
            "action": action_name,
            "description": desc,
            "inputSchema": input_schema,
            "hint": "再次调用 explore_mcp(path=...), 带上 params 参数即可执行此 action。",
        })

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
        pass

    def _register_server_tools(self, server_id: str, tools: Dict[str, Dict[str, Any]]) -> None:
        pass

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
