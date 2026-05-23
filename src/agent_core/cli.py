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
from agent_core.utils.yaml_subset import load_yaml_subset
from agent_core.utils.process import pid_exists, terminate_pid


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
    if old_pid and pid_exists(old_pid):
        host = (rt.get("host") or args.host or "127.0.0.1").strip()
        port = int(rt.get("port") or 0)
        url = _gateway_url(host, port) if port > 0 else None
        print(json.dumps({"ok": True, "already_running": True, "pid": old_pid, "url": url}, ensure_ascii=False))
        return 0

    host = (args.host or "127.0.0.1").strip()
    port = int(args.port or 0)
    if port <= 0:
        port = _pick_free_port(host)

    run_py = _find_run_py()
    cmd = [
        sys.executable,
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
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

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
    terminate_pid(pid)
    rt["running"] = False
    rt["stopped_at"] = int(time.time())
    _atomic_write_json(rt_path, rt)
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
        client = MCPHttpSSEClient(s)
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
        description="NanoGhost: LLM Agent 核心框架 — 多实例 / 飞书通道 / MCP 集成 / Gateway",
    )
    sub = parser.add_subparsers(dest="cmd")

    inst = sub.add_parser("instance", help="管理实例目录（列表 / 查路径）")
    inst_sub = inst.add_subparsers(dest="inst_cmd")
    inst_list = inst_sub.add_parser("list", help="列出所有实例")
    inst_list.set_defaults(func=_cmd_instance_list)
    inst_path = inst_sub.add_parser("path", help="解析实例名→绝对路径")
    inst_path.add_argument("name", help="实例名称")
    inst_path.set_defaults(func=_cmd_instance_path)

    gw = sub.add_parser("gateway", help="托管飞书 worker 的常驻后台进程")
    gw_sub = gw.add_subparsers(dest="gw_cmd")

    gw_start = gw_sub.add_parser("start", help="启动 gateway（后台 daemon）")
    gw_start.add_argument("--instance-dir", "-I", default=None, help="实例目录（路径或名称）")
    gw_start.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    gw_start.add_argument("--port", type=int, default=0, help="监听端口（0=自动分配）")
    gw_start.set_defaults(func=_cmd_gateway_start)

    gw_status = gw_sub.add_parser("status", help="查看 gateway 运行状态")
    gw_status.add_argument("--instance-dir", "-I", default=None, help="实例目录（路径或名称）")
    gw_status.add_argument("--host", default=None, help="网关地址（从 runtime 读取，通常不传）")
    gw_status.add_argument("--port", type=int, default=0, help="网关端口（从 runtime 读取，通常不传）")
    gw_status.set_defaults(func=_cmd_gateway_status)

    gw_stop = gw_sub.add_parser("stop", help="停止 gateway 及其托管的 worker")
    gw_stop.add_argument("--instance-dir", "-I", default=None, help="实例目录（路径或名称）")
    gw_stop.add_argument("--host", default=None, help="网关地址（从 runtime 读取，通常不传）")
    gw_stop.add_argument("--port", type=int, default=0, help="网关端口（从 runtime 读取，通常不传）")
    gw_stop.set_defaults(func=_cmd_gateway_stop)

    gw_restart = gw_sub.add_parser("restart", help="重启 gateway（先 stop 再 start）")
    gw_restart.add_argument("--instance-dir", "-I", default=None, help="实例目录（路径或名称）")
    gw_restart.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1）")
    gw_restart.add_argument("--port", type=int, default=0, help="监听端口（0=自动分配）")
    gw_restart.set_defaults(func=_cmd_gateway_restart)

    gw_health = gw_sub.add_parser("health", help="检查 gateway HTTP 健康端点")
    gw_health.add_argument("--instance-dir", "-I", default=None, help="实例目录（路径或名称）")
    gw_health.add_argument("--host", default=None, help="网关地址（从 runtime 读取，通常不传）")
    gw_health.add_argument("--port", type=int, default=0, help="网关端口（从 runtime 读取，通常不传）")
    gw_health.set_defaults(func=_cmd_gateway_health)

    mcp = sub.add_parser("mcp", help="MCP 服务器管理（注册表 / 探测 / 工具列表 / 重载）")
    mcp_sub = mcp.add_subparsers(dest="mcp_cmd")

    p_list = mcp_sub.add_parser("list", help="列出全局 MCP 服务器配置")
    p_list.set_defaults(func=_cmd_mcp_list)

    p_probe = mcp_sub.add_parser("probe", help="探测 MCP 服务器是否可达")
    p_probe.add_argument("--server", default=None, help="服务端 ID（不传则全量探测）")
    p_probe.add_argument("--instance-dir", "-I", default=None, help="实例目录（路径或名称）")
    p_probe.set_defaults(func=_cmd_mcp_probe)

    p_tools = mcp_sub.add_parser("tools", help="列出 MCP 服务器的可用工具")
    p_tools.add_argument("server_id", help="服务端 ID（如 lark-calendar）")
    p_tools.add_argument("--instance-dir", "-I", default=None, help="实例目录（路径或名称）")
    p_tools.set_defaults(func=_cmd_mcp_tools)

    p_reload = mcp_sub.add_parser("reload", help="重连所有 MCP 服务器并刷新工具列表")
    p_reload.add_argument("--instance-dir", "-I", default=None, help="实例目录（路径或名称）")
    p_reload.set_defaults(func=_cmd_mcp_reload)

    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    return int(func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
