import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

from agent_core.mcp.config import global_config_path, load_global_registry, mask_headers, resolve_servers
from agent_core.mcp.http_sse import MCPHttpSSEClient
from agent_core.utils import load_yaml_subset, pid_exists, terminate_pid


def _instances_root() -> Path:
    root = (os.getenv("NANOGHOST_INSTANCES_ROOT") or "").strip()
    if root:
        return Path(os.path.expanduser(root)).resolve()
    return (Path.home() / ".nanoghost" / "instances").resolve()


def _instance_dir_from_args(args) -> Path:
    inst = args.instance_dir or os.getenv("INSTANCE_DIR") or ""
    inst = inst.strip()
    if not inst:
        raise SystemExit("需要指定实例目录：-I <INSTANCE_DIR> 或设置 INSTANCE_DIR")
    p = Path(os.path.expanduser(inst))
    if p.is_absolute() or p.exists() or any(x in inst for x in ("/", "\\", ":")):
        return Path(os.path.abspath(str(p)))
    return (_instances_root() / inst).resolve()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _find_run_py() -> Path:
    p = os.getenv("NANOGHOST_RUNPY") or ""
    p = p.strip()
    if p:
        pp = Path(p).expanduser().resolve()
        if pp.is_file():
            return pp
    cur = Path.cwd().resolve()
    for _ in range(12):
        cand = cur / "run.py"
        if cand.is_file():
            return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    raise SystemExit("找不到 run.py，请在仓库目录运行或设置 NANOGHOST_RUNPY=<run.py绝对路径>")


def _find_venv_python() -> str:
    """优先使用 run.py 同级目录下 venv 里的 Python，回退到 sys.executable。"""
    run_py = _find_run_py()
    repo_root = run_py.parent
    if os.name == "nt":
        venv_python = repo_root / "venv" / "Scripts" / "python.exe"
    else:
        venv_python = repo_root / "venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python)
    return sys.executable


def _gateway_runtime_path(inst: Path) -> Path:
    return inst / "runtime" / "gateway.json"


def _gateway_url(host: str, port: int) -> str:
    return f"http://{host}:{int(port)}"


def _pick_free_port(host: str) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return int(s.getsockname()[1])
    finally:
        try:
            s.close()
        except Exception:
            pass


def _ensure_instance_layout(inst: Path) -> None:
    inst.mkdir(parents=True, exist_ok=True)
    (inst / "runtime").mkdir(parents=True, exist_ok=True)
    (inst / "data").mkdir(parents=True, exist_ok=True)
    (inst / "work").mkdir(parents=True, exist_ok=True)
    (inst / "prompts").mkdir(parents=True, exist_ok=True)
    (inst / "skills").mkdir(parents=True, exist_ok=True)
    ch_path = inst / "channel_directory.json"
    if not ch_path.exists():
        _atomic_write_json(ch_path, {"updated_at": None, "channels": {"feishu": {"enabled": False}}})



def _cmd_gateway_start(args) -> int:
    inst = _instance_dir_from_args(args)
    _ensure_instance_layout(inst)
    rt_path = _gateway_runtime_path(inst)
    rt = _read_json(rt_path)
    old_pid = int(rt.get("pid") or 0)
    rt_running = bool(rt.get("running"))
    if old_pid and pid_exists(old_pid) and rt_running:
        host = (rt.get("host") or args.host or "127.0.0.1").strip()
        port = int(rt.get("port") or 0)
        url = _gateway_url(host, port) if port > 0 else None
        print(json.dumps({"ok": True, "already_running": True, "pid": old_pid, "url": url}, ensure_ascii=False))
        return 0
    if old_pid and pid_exists(old_pid) and not rt_running:
        # stale pid in runtime file — try to clean up before starting fresh
        terminate_pid(old_pid)

    host = (args.host or "127.0.0.1").strip()
    port = int(args.port or 0)
    if port <= 0:
        port = _pick_free_port(host)

    run_py = _find_run_py()
    python_exe = _find_venv_python()
    cmd = [
        python_exe,
        str(run_py),
        "--gateway",
        "-I",
        str(inst),
        "--host",
        host,
        "--port",
        str(port),
    ]

    env = dict(os.environ)
    env["INSTANCE_DIR"] = str(inst)
    env.setdefault("PYTHONUNBUFFERED", "1")

    popen_kwargs = {
        "cwd": str(inst),
        "env": env,
        "stdin": subprocess.DEVNULL,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    else:
        if os.environ.get("NANOGHOST_CALLER", "").strip().lower() != "openobstrator":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        else:
            log_dir = inst / "runtime"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "gateway_stdout.log"
            log_fd = open(log_path, "a", encoding="utf-8")
            popen_kwargs["stdout"] = log_fd
            popen_kwargs["stderr"] = subprocess.STDOUT
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    p = subprocess.Popen(cmd, **popen_kwargs)
    rt = {
        "running": True,
        "pid": int(p.pid),
        "host": host,
        "port": int(port),
        "started_at": int(time.time()),
        "cmd": cmd,
    }
    _atomic_write_json(rt_path, rt)

    base = _gateway_url(host, port)
    ok = False
    last_err = ""
    for _ in range(40):
        try:
            r = requests.get(base + "/api/health", timeout=0.5)
            if r.status_code == 200:
                ok = True
                break
        except Exception as e:
            last_err = str(e)
        time.sleep(0.25)

    if ok:
        try:
            requests.post(base + "/api/start", timeout=2)
        except Exception:
            pass
        print(json.dumps({"ok": True, "pid": int(p.pid), "url": base}, ensure_ascii=False))
        return 0

    rt["running"] = False
    rt["last_error"] = last_err or "gateway not reachable"
    _atomic_write_json(rt_path, rt)
    print(json.dumps({"ok": False, "pid": int(p.pid), "error": rt["last_error"]}, ensure_ascii=False))
    return 2


def _cmd_gateway_status(args) -> int:
    inst = _instance_dir_from_args(args)
    rt = _read_json(_gateway_runtime_path(inst))
    pid = int(rt.get("pid") or 0)
    host = (rt.get("host") or args.host or "127.0.0.1").strip()
    port = int(rt.get("port") or args.port or 0)
    running = bool(rt.get("running")) and pid_exists(pid)

    out = {"ok": True, "running": running, "pid": pid or None, "host": host, "port": port}
    if running and port > 0:
        try:
            r = requests.get(_gateway_url(host, port) + "/api/status", timeout=2)
            if r.status_code == 200:
                out["status"] = r.json()
        except Exception as e:
            out["status_error"] = str(e)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _cmd_gateway_stop(args) -> int:
    inst = _instance_dir_from_args(args)
    _ensure_instance_layout(inst)
    rt_path = _gateway_runtime_path(inst)
    rt = _read_json(rt_path)
    pid = int(rt.get("pid") or 0)
    host = (rt.get("host") or args.host or "127.0.0.1").strip()
    port = int(rt.get("port") or args.port or 0)
    if port > 0:
        try:
            requests.post(_gateway_url(host, port) + "/api/stop", timeout=2)
        except Exception:
            pass
    killed = terminate_pid(pid)
    rt["running"] = False
    rt["pid"] = None
    rt["stopped_at"] = int(time.time())
    _atomic_write_json(rt_path, rt)
    if pid and not killed:
        print(json.dumps({"ok": True, "pid": pid or None, "warning": f"could not terminate pid {pid}, pid field cleared anyway"}, ensure_ascii=False))
    else:
        print(json.dumps({"ok": True, "pid": pid or None}, ensure_ascii=False))
    return 0


def _cmd_gateway_restart(args) -> int:
    inst = _instance_dir_from_args(args)
    _ensure_instance_layout(inst)
    rt_path = _gateway_runtime_path(inst)
    rt = _read_json(rt_path)
    pid = int(rt.get("pid") or 0)
    host = (rt.get("host") or args.host or "127.0.0.1").strip()
    port = int(rt.get("port") or args.port or 0)
    if port > 0:
        try:
            r = requests.post(_gateway_url(host, port) + "/api/restart", timeout=2)
            if r.status_code == 200:
                print(json.dumps({"ok": True, "result": r.json()}, ensure_ascii=False))
                return 0
        except Exception as e:
            pass
    terminate_pid(pid)
    if not port or port <= 0:
        print(json.dumps({"ok": False, "error": "gateway not running, no port known"}, ensure_ascii=False))
        return 2
    time.sleep(0.5)
    return _cmd_gateway_start(args)


def _cmd_gateway_health(args) -> int:
    inst = _instance_dir_from_args(args)
    rt_path = _gateway_runtime_path(inst)
    rt = _read_json(rt_path)
    pid = int(rt.get("pid") or 0)
    host = (rt.get("host") or args.host or "127.0.0.1").strip()
    port = int(rt.get("port") or args.port or 0)
    running = bool(rt.get("running")) and pid_exists(pid)

    if not running or port <= 0:
        print(json.dumps({"ok": False, "running": False, "error": "gateway not running"}, ensure_ascii=False))
        return 1

    try:
        r = requests.get(_gateway_url(host, port) + "/api/health", timeout=2)
        if r.status_code == 200:
            data = r.json()
            data["ok"] = True
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
    except Exception as e:
        print(json.dumps({"ok": False, "running": True, "error": str(e)}, ensure_ascii=False))
        return 2
    return 1


def _cmd_instance_list(_args) -> int:
    root = _instances_root()
    root.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir():
            continue
        items.append({"name": p.name, "path": str(p)})
    print(json.dumps({"root": str(root), "instances": items}, ensure_ascii=False, indent=2))
    return 0


def _cmd_instance_delete(args) -> int:
    root = _instances_root()
    name = str(args.name).strip()
    if not name:
        raise SystemExit("instance name is required")
    inst = (root / name).resolve()
    if not inst.exists():
        print(json.dumps({"ok": False, "name": name, "path": str(inst), "error": "not found"}, ensure_ascii=False))
        return 1
    import shutil
    shutil.rmtree(inst)
    print(json.dumps({"ok": True, "name": name, "path": str(inst)}, ensure_ascii=False))
    return 0


def _cmd_instance_create(args) -> int:
    root = _instances_root()
    name = str(args.name).strip()
    if not name:
        raise SystemExit("instance name is required")
    inst = (root / name).resolve()
    if inst.exists():
        print(json.dumps({"ok": False, "name": name, "path": str(inst), "error": "already exists"}, ensure_ascii=False))
        return 1
    _ensure_instance_layout(inst)
    repo = Path(__file__).resolve().parent.parent.parent
    for src in [repo / ".env.example", repo / "prompts" / "agent_profile.md", repo / "prompts" / "agent_rules_conduct.md"]:
        if src.is_file():
            dst = inst / src.name
            if not dst.exists():
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(json.dumps({"ok": True, "name": name, "path": str(inst)}, ensure_ascii=False))
    return 0


def _cmd_instance_show(args) -> int:
    root = _instances_root()
    name = str(args.name).strip()
    inst = (root / name).resolve()
    if not inst.exists():
        print(json.dumps({"ok": False, "error": "instance not found", "name": name}, ensure_ascii=False))
        return 1

    # load .env for accurate env display
    from dotenv import load_dotenv
    inst_env = inst / ".env"
    if inst_env.is_file():
        load_dotenv(inst_env, override=True)
    load_dotenv(override=False)  # root .env as fallback

    out: dict = {"ok": True, "name": name, "path": str(inst)}

    # channel config
    ch_path = inst / "channel_directory.json"
    if ch_path.exists():
        out["channel_config"] = _read_json(ch_path)

    # runtime (gateway + workers)
    rt_dir = inst / "runtime"
    if rt_dir.is_dir():
        out["runtime"] = {}
        for f in sorted(rt_dir.iterdir()):
            if f.suffix == ".json":
                out["runtime"][f.stem] = _read_json(f)

    # env check
    env_info: dict = {}
    for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "AGENT_MODE", "NO_PROXY"):
        v = os.environ.get(k, "")
        if k == "FEISHU_APP_SECRET":
            env_info[k] = "***set***" if v else "(not set)"
        else:
            env_info[k] = v or "(not set)"
    out["env"] = env_info

    # dirs
    out["dirs"] = {}
    for d in ("data", "work", "prompts", "skills"):
        p = inst / d
        out["dirs"][d] = {"exists": p.is_dir(), "path": str(p)}

    # prompts
    prompts_dir = inst / "prompts"
    if prompts_dir.is_dir():
        prompts = sorted(f.name for f in prompts_dir.iterdir() if f.is_file())
        out["prompts"] = prompts

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _cmd_instance_set_channel(args) -> int:
    root = _instances_root()
    name = str(args.name).strip()
    channel = str(args.channel).strip()
    enabled = str(args.enabled).lower() in ("1", "true", "yes", "on")
    inst = (root / name).resolve()
    if not inst.exists():
        print(json.dumps({"ok": False, "error": "instance not found", "name": name}, ensure_ascii=False))
        return 1
    ch_path = inst / "channel_directory.json"
    cfg = _read_json(ch_path) if ch_path.exists() else {"updated_at": None, "channels": {}}
    cfg.setdefault("channels", {})
    cfg["channels"][channel] = {"enabled": enabled}
    cfg["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _atomic_write_json(ch_path, cfg)
    print(json.dumps({"ok": True, "name": name, "channel": channel, "enabled": enabled, "path": str(ch_path)}, ensure_ascii=False))
    return 0


def _cmd_instance_path(args) -> int:
    root = _instances_root()
    p = (root / str(args.name)).resolve()
    print(json.dumps({"ok": True, "name": args.name, "path": str(p)}, ensure_ascii=False))
    return 0


def _cmd_mcp_list(_args) -> int:
    cfg = load_global_registry()
    reg = cfg.get("mcp_servers") if isinstance(cfg, dict) else {}
    reg = reg if isinstance(reg, dict) else {}
    out = []
    for sid, s in sorted(reg.items(), key=lambda x: str(x[0])):
        if not isinstance(s, dict):
            continue
        out.append(
            {
                "id": sid,
                "enabled": bool(s.get("enabled", True)),
                "transport": s.get("transport") or "http_sse",
                "url": s.get("url") or "",
                "headers": mask_headers(s.get("headers") or {}) if isinstance(s.get("headers"), dict) else {},
                "timeout_seconds": int(s.get("timeout_seconds") or 30),
            }
        )
    print(json.dumps({"config_path": str(global_config_path()), "mcp_servers": out}, ensure_ascii=False, indent=2))
    return 0


def _iter_probe_targets(args) -> list[str]:
    cfg = load_global_registry()
    reg = cfg.get("mcp_servers") if isinstance(cfg, dict) else {}
    reg = reg if isinstance(reg, dict) else {}
    if args.server:
        return [args.server]
    return [str(k) for k, v in reg.items() if isinstance(v, dict) and v.get("enabled", True) is not False]


def _cmd_mcp_probe(args) -> int:
    inst = _instance_dir_from_args(args)
    servers = {s.server_id: s for s in resolve_servers(inst)}
    targets = _iter_probe_targets(args)
    results = []
    for sid in targets:
        s = servers.get(sid)
        if not s:
            results.append({"server_id": sid, "ok": False, "status": "disabled_or_not_allowed"})
            continue
        from agent_core.mcp.stdio_client import MCPStdioClient
        client = MCPStdioClient(s) if s.transport == "stdio" else MCPHttpSSEClient(s)
        r = client.probe()
        results.append({"server_id": sid, "ok": r.ok, "status": r.status, "error": r.error, "duration_ms": r.duration_ms})
    print(json.dumps({"instance_dir": str(inst), "results": results}, ensure_ascii=False, indent=2))
    return 0


def _cmd_mcp_tools(args) -> int:
    inst = _instance_dir_from_args(args)
    servers = {s.server_id: s for s in resolve_servers(inst)}
    s = servers.get(args.server_id)
    if not s:
        raise SystemExit(f"server 不可用或不在白名单中: {args.server_id}")
    client = MCPHttpSSEClient(s)
    ok, result, err, dur = client.list_tools()
    tools = result.get("tools") if isinstance(result, dict) else None
    payload = {
        "server_id": s.server_id,
        "ok": ok,
        "duration_ms": dur,
        "error": err,
        "tools": tools if isinstance(tools, list) else [],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 2


def _cmd_mcp_reload(args) -> int:
    inst = _instance_dir_from_args(args)
    servers = resolve_servers(inst)
    results = []
    for s in servers:
        client = MCPHttpSSEClient(s)
        pr = client.probe()
        ok, tool_res, err, dur = client.list_tools() if pr.ok else (False, None, pr.error, pr.duration_ms)
        tools = tool_res.get("tools") if isinstance(tool_res, dict) else None
        results.append(
            {
                "server_id": s.server_id,
                "probe_ok": pr.ok,
                "status": pr.status,
                "error": err,
                "duration_ms": int(dur or 0),
                "tools_count": len(tools) if isinstance(tools, list) else 0,
            }
        )
    print(json.dumps({"instance_dir": str(inst), "results": results}, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nanoghost",
        description="NanoGhost -- multi-instance LLM Agent framework with Feishu/MCP/Gateway support",
    )
    sub = parser.add_subparsers(dest="cmd")

    inst = sub.add_parser("instance", help="manage instance directories (create / list / resolve path)")
    inst_sub = inst.add_subparsers(dest="inst_cmd")
    inst_create = inst_sub.add_parser("create", help="create a new instance with default layout")
    inst_create.add_argument("name", help="instance name (e.g. capture)")
    inst_create.set_defaults(func=_cmd_instance_create)
    inst_delete = inst_sub.add_parser("delete", help="delete an instance (removes all data)")
    inst_delete.add_argument("name", help="instance name (e.g. capture)")
    inst_delete.set_defaults(func=_cmd_instance_delete)
    inst_list = inst_sub.add_parser("list", help="list all instances")
    inst_list.set_defaults(func=_cmd_instance_list)
    inst_path = inst_sub.add_parser("path", help="resolve instance name to absolute path")
    inst_path.add_argument("name", help="instance name")
    inst_path.set_defaults(func=_cmd_instance_path)

    inst_show = inst_sub.add_parser("show", help="show full instance configuration (channels, runtime, env, dirs)")
    inst_show.add_argument("name", help="instance name")
    inst_show.set_defaults(func=_cmd_instance_show)

    inst_set_ch = inst_sub.add_parser("set-channel", help="enable/disable a channel for an instance")
    inst_set_ch.add_argument("name", help="instance name")
    inst_set_ch.add_argument("channel", help="channel name (e.g. feishu)")
    inst_set_ch.add_argument("enabled", help="1/0, true/false, yes/no")
    inst_set_ch.set_defaults(func=_cmd_instance_set_channel)

    gw = sub.add_parser("gateway", help="daemon process that manages Feishu worker lifecycle")
    gw_sub = gw.add_subparsers(dest="gw_cmd")

    gw_start = gw_sub.add_parser("start", help="start gateway as a background daemon")
    gw_start.add_argument("--instance-dir", "-I", default=None, help="instance directory (path or name)")
    gw_start.add_argument("--host", default="127.0.0.1", help="listen address (default 127.0.0.1)")
    gw_start.add_argument("--port", type=int, default=0, help="listen port (0=auto assign)")
    gw_start.set_defaults(func=_cmd_gateway_start)

    gw_status = gw_sub.add_parser("status", help="show gateway runtime status")
    gw_status.add_argument("--instance-dir", "-I", default=None, help="instance directory (path or name)")
    gw_status.add_argument("--host", default=None, help="gateway address (read from runtime, usually omit)")
    gw_status.add_argument("--port", type=int, default=0, help="gateway port (read from runtime, usually omit)")
    gw_status.set_defaults(func=_cmd_gateway_status)

    gw_stop = gw_sub.add_parser("stop", help="stop gateway and its managed workers")
    gw_stop.add_argument("--instance-dir", "-I", default=None, help="instance directory (path or name)")
    gw_stop.add_argument("--host", default=None, help="gateway address (read from runtime, usually omit)")
    gw_stop.add_argument("--port", type=int, default=0, help="gateway port (read from runtime, usually omit)")
    gw_stop.set_defaults(func=_cmd_gateway_stop)

    gw_restart = gw_sub.add_parser("restart", help="restart gateway (stop then start)")
    gw_restart.add_argument("--instance-dir", "-I", default=None, help="instance directory (path or name)")
    gw_restart.add_argument("--host", default=None, help="listen address (default 127.0.0.1)")
    gw_restart.add_argument("--port", type=int, default=0, help="listen port (0=auto assign)")
    gw_restart.set_defaults(func=_cmd_gateway_restart)

    gw_health = gw_sub.add_parser("health", help="check gateway HTTP health endpoint")
    gw_health.add_argument("--instance-dir", "-I", default=None, help="instance directory (path or name)")
    gw_health.add_argument("--host", default=None, help="gateway address (read from runtime, usually omit)")
    gw_health.add_argument("--port", type=int, default=0, help="gateway port (read from runtime, usually omit)")
    gw_health.set_defaults(func=_cmd_gateway_health)

    mcp = sub.add_parser("mcp", help="MCP server management (registry / probe / tools / reload)")
    mcp_sub = mcp.add_subparsers(dest="mcp_cmd")

    p_list = mcp_sub.add_parser("list", help="list global MCP server config")
    p_list.set_defaults(func=_cmd_mcp_list)

    p_probe = mcp_sub.add_parser("probe", help="probe MCP server connectivity")
    p_probe.add_argument("--server", default=None, help="server ID (omit to probe all enabled)")
    p_probe.add_argument("--instance-dir", "-I", default=None, help="instance directory (path or name)")
    p_probe.set_defaults(func=_cmd_mcp_probe)

    p_tools = mcp_sub.add_parser("tools", help="list tools from an MCP server")
    p_tools.add_argument("server_id", help="server ID (e.g. lark-calendar)")
    p_tools.add_argument("--instance-dir", "-I", default=None, help="instance directory (path or name)")
    p_tools.set_defaults(func=_cmd_mcp_tools)

    p_reload = mcp_sub.add_parser("reload", help="reconnect all MCP servers and refresh tool list")
    p_reload.add_argument("--instance-dir", "-I", default=None, help="instance directory (path or name)")
    p_reload.set_defaults(func=_cmd_mcp_reload)

    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return int(func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
