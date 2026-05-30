import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .yaml_subset import load_yaml_subset, substitute_env


def _default_global_config_path() -> Path:
    home = Path.home()
    if os.name == "nt":
        return home / ".nanoghost" / "config.yaml"
    return home / ".nanoghost" / "config.yaml"


def global_config_path() -> Path:
    p = os.getenv("NANOGHOST_GLOBAL_CONFIG") or ""
    p = p.strip()
    return Path(p).expanduser().resolve() if p else _default_global_config_path()


def instance_config_path(instance_dir: Path) -> Path:
    return instance_dir / "config.yaml"


@dataclass(frozen=True)
class MCPServerConfig:
    server_id: str
    transport: str
    url: str
    headers: Dict[str, str]
    timeout_seconds: int
    extra_args: List[str] = None  # type: ignore

    def __post_init__(self):
        if self.extra_args is None:
            object.__setattr__(self, 'extra_args', [])


def load_global_registry() -> Dict[str, Any]:
    return load_yaml_subset(global_config_path())


def load_instance_config(instance_dir: Path) -> Dict[str, Any]:
    return load_yaml_subset(instance_config_path(instance_dir))


def _as_dict(obj: Any) -> Dict[str, Any]:
    return obj if isinstance(obj, dict) else {}


def _as_list(obj: Any) -> List[Any]:
    return obj if isinstance(obj, list) else []


def effective_server_ids(instance_dir: Path, global_cfg: Dict[str, Any]) -> List[str]:
    # 从 YAML 读取 enabled_only，不要用 load_instance_config（返回 dataclass）
    from agent_core.utils.yaml_subset import load_yaml_subset
    yaml_data = load_yaml_subset(instance_dir / "config.yaml") or {}
    mcp = _as_dict(yaml_data.get("mcp"))
    enabled_only = _as_list(mcp.get("enabled_only"))
    enabled_only = [str(x).strip() for x in enabled_only if str(x).strip()]
    if not enabled_only:
        return []

    reg = _as_dict(global_cfg.get("mcp_servers"))
    out: List[str] = []
    for sid in enabled_only:
        s = _as_dict(reg.get(sid))
        if not s:
            continue
        if s.get("enabled") is False:
            continue
        out.append(sid)
    return out


def resolve_servers(instance_dir: Path) -> List[MCPServerConfig]:
    global_cfg = load_global_registry()
    reg = _as_dict(global_cfg.get("mcp_servers"))
    allow = set(effective_server_ids(instance_dir, global_cfg))
    if not allow:
        return []

    out: List[MCPServerConfig] = []
    for sid in sorted(allow):
        s = _as_dict(reg.get(sid))
        if not s:
            continue
        transport = str(s.get("transport") or "http_sse").strip()
        if transport == "stdio":
            url = substitute_env(str(s.get("command") or "")).strip()
            args_raw = _as_list(s.get("args"))
            extra_args = [substitute_env(str(a)) for a in args_raw if str(a).strip()]
        else:
            url = substitute_env(str(s.get("url") or "")).strip()
            extra_args = []
        if not url:
            continue
        timeout_seconds = int(s.get("timeout_seconds") or 30)
        headers_raw = _as_dict(s.get("headers"))
        headers: Dict[str, str] = {}
        for hk, hv in headers_raw.items():
            if hv is None:
                continue
            headers[str(hk)] = substitute_env(str(hv))
        out.append(
            MCPServerConfig(
                server_id=sid,
                transport=transport,
                url=url,
                headers=headers,
                timeout_seconds=timeout_seconds,
                extra_args=extra_args,
            )
        )
    return out


def mask_headers(headers: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        if k.lower() == "authorization":
            vv = (v or "").strip()
            if vv.lower().startswith("bearer "):
                out[k] = "Bearer ***"
            else:
                out[k] = "***"
        else:
            out[k] = v
    return out

