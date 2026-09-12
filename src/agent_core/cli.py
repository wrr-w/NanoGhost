import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

from agent_core.mcp_client.config import global_config_path, load_global_registry, mask_headers, resolve_servers
from agent_core.mcp_client.http_sse import MCPHttpSSEClient
from agent_core.mcp_client.manifest_cache import load_manifest_cache
from agent_core.setup_wizard import ensure_llm_configured, _env_path as _wizard_env_path, _write_env, _read_env
from agent_core.update import check_for_updates, apply_update, auto_check_and_notify
from agent_core.version import current_version, compare_versions
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
    # 绝对路径 / 含路径分隔符 → 直接使用
    p = Path(os.path.expanduser(inst))
    if p.is_absolute() or any(x in inst for x in ("/", "\\", ":")):
        return Path(os.path.abspath(str(p)))
    # 否则从配置的根目录查找/创建
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


_FROZEN = getattr(sys, "frozen", False)


def _find_run_py() -> Path:
    if _FROZEN:
        return Path(sys.executable)
    p = os.getenv("NANOGHOST_RUNPY") or ""
    p = p.strip()
    if p:
        pp = Path(p).expanduser().resolve()
        if pp.is_file():
            return pp
    # 从 CWD 向上搜索
    cur = Path.cwd().resolve()
    for _ in range(12):
        cand = cur / "run.py"
        if cand.is_file():
            return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    # FALLBACK: 从 exe 所在目录搜索（onedir 模式）
    exe_dir = Path(sys.argv[0]).resolve().parent
    cur = exe_dir
    for _ in range(12):
        cand = cur / "run.py"
        if cand.is_file():
            return cand
        if cur.parent == cur:
            break
        cur = cur.parent
    raise SystemExit("找不到 run.py，请在仓库目录运行或设置 NANOGHOST_RUNPY=<run.py绝对路径>")


def _find_venv_python() -> str:
    if _FROZEN:
        return sys.executable
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
    if _FROZEN:
        cmd = [sys.executable, "--gateway", "-I", str(inst), "--host", host, "--port", str(port)]
    else:
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
    if _FROZEN:
        repo = Path(sys._MEIPASS)
    else:
        repo = Path(__file__).resolve().parent.parent.parent
    for src in [repo / ".env.example", repo / "prompts" / "agent_profile.md", repo / "prompts" / "agent_rules_conduct.md"]:
        if src.is_file():
            dst = inst / (".env" if src.name == ".env.example" else src.name)
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
    # AGENT_MODE_SOURCE 是配套的：模式只有经过 run.py 的 bootstrap 才有值，看到
    # 它是空的就说明这个进程没走 bootstrap（模式可能是别处塞进来的）
    for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "AGENT_MODE", "AGENT_MODE_SOURCE", "NO_PROXY"):
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
        from agent_core.mcp_client.stdio_client import MCPStdioClient
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
    from agent_core.mcp_client.stdio_client import MCPStdioClient
    client = MCPStdioClient(s) if s.transport == "stdio" else MCPHttpSSEClient(s)
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
        from agent_core.mcp_client.stdio_client import MCPStdioClient
        client = MCPStdioClient(s) if s.transport == "stdio" else MCPHttpSSEClient(s)
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


def _cmd_mcp_manifest(args) -> int:
    inst = _instance_dir_from_args(args)
    servers = resolve_servers(inst)
    rows = []
    for s in servers:
        manifest = load_manifest_cache(inst, s.server_id) or {}
        actions = manifest.get("actions") if isinstance(manifest, dict) else None
        rows.append(
            {
                "server_id": s.server_id,
                "transport": s.transport,
                "url": s.url,
                "has_manifest": bool(manifest),
                "actions_count": len(actions) if isinstance(actions, list) else 0,
                "last_manifest_refresh_at": manifest.get("last_manifest_refresh_at") if isinstance(manifest, dict) else None,
            }
        )
    print(json.dumps({"instance_dir": str(inst), "results": rows}, ensure_ascii=False, indent=2))
    return 0


def _load_dotenv_frozen() -> None:
    """Load global .env from ~/.nanoghost/.env only."""
    from dotenv import load_dotenv
    global_env = os.path.join(os.path.expanduser("~"), ".nanoghost", ".env")
    if os.path.isfile(global_env):
        load_dotenv(dotenv_path=global_env, override=False)
    _normalize_llm_env()


def _normalize_llm_env() -> None:
    """兼容 OpenObstrator 的键名：OPENAI_API_KEY → LLM_API_KEY 等"""
    _mapping = [
        ("LLM_API_KEY", "OPENAI_API_KEY", "API_KEY"),
        ("LLM_BASE_URL", "OPENAI_BASE_URL", "BASE_URL"),
        ("LLM_MODEL", "OPENAI_MODEL", "MODEL_NAME"),
    ]
    for primary, *fallbacks in _mapping:
        if not (os.getenv(primary) or "").strip():
            for fb in fallbacks:
                v = (os.getenv(fb) or "").strip()
                if v:
                    os.environ[primary] = v
                    break


def _resolve_instance_arg(instance_dir_arg: str | None) -> str | None:
    if not instance_dir_arg:
        return None
    inst = instance_dir_arg.strip()
    p = Path(os.path.expanduser(inst))
    if p.is_absolute() or any(x in inst for x in ("/", "\\", ":")):
        return os.path.abspath(str(p))
    return str((_instances_root() / inst).resolve())


def _cmd_config_set(args) -> int:
    key = (args.key or "").strip().upper()
    value = (args.value or "").strip()
    if not key:
        print(json.dumps({"ok": False, "error": "key is required"}, ensure_ascii=False))
        return 1
    inst_dir = _resolve_instance_arg(getattr(args, "instance_dir", None))
    _write_env(key, value, instance_dir=inst_dir)
    os.environ[key] = value
    path = _wizard_env_path(inst_dir)
    print(json.dumps({"ok": True, "key": key, "value": value, "path": path}, ensure_ascii=False))
    return 0


def _cmd_config_get(args) -> int:
    key = (args.key or "").strip().upper()
    if not key:
        print(json.dumps({"ok": False, "error": "key is required"}, ensure_ascii=False))
        return 1
    inst_dir = _resolve_instance_arg(getattr(args, "instance_dir", None))
    val = _read_env(key, instance_dir=inst_dir) or os.getenv(key, "")
    print(json.dumps({"ok": True, "key": key, "value": val}, ensure_ascii=False))
    return 0


def _cmd_config_list(args) -> int:
    inst_dir = _resolve_instance_arg(getattr(args, "instance_dir", None))
    path = _wizard_env_path(inst_dir)
    _secret_keys = {"LLM_API_KEY", "FEISHU_APP_SECRET", "EMBED_API_KEY", "FEISHU_APP_ID"}
    items: dict = {}
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    if k in _secret_keys and v:
                        v = v[:4] + "***" + v[-4:] if len(v) > 8 else "***"
                    items[k] = v
    print(json.dumps({"ok": True, "path": path, "env": items}, ensure_ascii=False, indent=2))
    return 0


# update 子命令的退出码约定（外部管理器依赖这三个值分支，不要随意改动）
UPDATE_EXIT_OK = 0          # 已是最新，无需更新
UPDATE_EXIT_AVAILABLE = 10  # 有新版本：--check 时表示"可用"；不带 --check 时表示"覆盖脚本已启动"
UPDATE_EXIT_ERROR = 1       # 失败（网络 / 未配置 repo / 下载或启动失败）


def _cmd_update(args) -> int:
    """检查/执行升级。

    脚本化调用约定（见 docs/UPDATING.md）：

        nanoghost update --check --json
            0  = 已是最新        10 = 有新版本        1 = 检查失败

        nanoghost update --yes --no-restart --json
            0  = 已是最新，什么都没做
            10 = 下载完成、覆盖脚本已在后台启动（**覆盖尚未发生**）
            1  = 失败，已下载的包会保留

    注意 exit 10 不代表覆盖已成功：覆盖在本进程退出之后才执行。调用方需要轮询
    result_file（默认 %TEMP%\\nanoghost_update_result.txt）：
        "OK"              成功
        "FAIL:timeout"    程序没能在 60 秒内退出
        "FAIL:expand"     安装包解压失败
        "FAIL:copy"       覆盖安装目录失败（exe 被占用）
    """
    from agent_core.update import check_for_updates, apply_update, update_result_path
    from agent_core.version import current_version

    as_json = bool(getattr(args, "json", False))
    check_only = bool(getattr(args, "check", False))
    non_interactive = bool(getattr(args, "yes", False)) or not sys.stdin.isatty()
    restart = not bool(getattr(args, "no_restart", False))

    def emit(payload: dict, human: str = "") -> None:
        if as_json:
            print(json.dumps(payload, ensure_ascii=False))
        elif human:
            print(human)

    local = current_version()
    has_update, remote, url_or_err = check_for_updates()

    # check_for_updates 失败时 remote 为空；"已是最新"时 remote 有值
    if not has_update and not remote:
        emit({"ok": False, "error": url_or_err, "current_version": local}, url_or_err)
        return UPDATE_EXIT_ERROR

    if not has_update:
        emit(
            {"ok": True, "update_available": False,
             "current_version": local, "latest_version": remote},
            f"已是最新版本 v{local}",
        )
        return UPDATE_EXIT_OK

    if check_only:
        emit(
            {"ok": True, "update_available": True,
             "current_version": local, "latest_version": remote,
             "download_url": url_or_err},
            f"发现新版本 v{remote} (当前 v{local})",
        )
        return UPDATE_EXIT_AVAILABLE

    ok, err = apply_update(url_or_err, interactive=not non_interactive, restart=restart)
    if not ok:
        emit({"ok": False, "error": err, "current_version": local}, f"升级失败: {err}")
        return UPDATE_EXIT_ERROR

    emit(
        {"ok": True, "update_available": True, "applied": "started",
         "current_version": local, "latest_version": remote,
         "restart": restart, "result_file": update_result_path()},
        f"v{remote} 覆盖脚本已启动，本进程退出后执行。结果见 {update_result_path()}",
    )
    return UPDATE_EXIT_AVAILABLE


def _cmd_setup_wizard(_args) -> int:
    from agent_core.setup_wizard import run_setup_wizard as _run_w
    ok = _run_w()
    print(json.dumps({"ok": ok}, ensure_ascii=False))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_frozen()
    parser = argparse.ArgumentParser(
        prog="nanoghost",
        description="NanoGhost -- multi-instance LLM Agent framework with Feishu/MCP/Gateway support",
    )
    parser.add_argument("--gateway", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--instance-dir", "-I", default=None, help=argparse.SUPPRESS)
    # 只读本地 VERSION 文件，不发网络请求 —— 外部管理器用它确认"升级是否真的
    # 落地了"。用 update --check 也能拿到 current_version，但那要联网，机器离线时
    # 就确认不了。
    parser.add_argument("--version", "-V", action="store_true",
                        help="print current version and exit")
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

    p_manifest = mcp_sub.add_parser("manifest", help="show local MCP manifest cache")
    p_manifest.add_argument("--instance-dir", "-I", default=None, help="instance directory (path or name)")
    p_manifest.set_defaults(func=_cmd_mcp_manifest)

    cfg = sub.add_parser("config", help="manage .env configuration (set/get/list)")
    cfg_sub = cfg.add_subparsers(dest="cfg_cmd")
    cfg_set = cfg_sub.add_parser("set", help="set an env variable")
    cfg_set.add_argument("key", help="variable name (e.g. LLM_API_KEY)")
    cfg_set.add_argument("value", help="variable value")
    cfg_set.add_argument("--instance-dir", "-I", default=None, help="target instance directory")
    cfg_set.set_defaults(func=_cmd_config_set)
    cfg_get = cfg_sub.add_parser("get", help="get an env variable value")
    cfg_get.add_argument("key", help="variable name")
    cfg_get.add_argument("--instance-dir", "-I", default=None, help="target instance directory")
    cfg_get.set_defaults(func=_cmd_config_get)
    cfg_list = cfg_sub.add_parser("list", help="list all .env entries")
    cfg_list.add_argument("--instance-dir", "-I", default=None, help="target instance directory")
    cfg_list.set_defaults(func=_cmd_config_list)

    upd = sub.add_parser("update", help="检查并升级到最新版本 (GitHub Releases)")
    upd.add_argument("--check", action="store_true", help="只检查是否有新版本，不下载")
    upd.add_argument("--yes", "-y", action="store_true", help="非交互：不打印进度、失败不暂停（脚本/管理器调用）")
    upd.add_argument("--no-restart", action="store_true", help="覆盖完成后不自动拉起程序，交给外部管理器")
    upd.add_argument("--json", action="store_true", help="只输出 JSON，便于脚本解析")
    upd.set_defaults(func=_cmd_update)

    setup = sub.add_parser("setup", help="交互式配置向导（首次运行）")
    setup.set_defaults(func=_cmd_setup_wizard)

    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        print(current_version())
        return 0
    if args.gateway:
        if not args.port or int(args.port) <= 0:
            parser.error("--port required with --gateway")
        inst_raw = args.instance_dir or os.getenv("INSTANCE_DIR") or ""
        inst_raw = inst_raw.strip()
        if not inst_raw:
            parser.error("--instance-dir/-I required with --gateway")
        p = Path(os.path.expanduser(inst_raw))
        if p.is_absolute() or p.exists() or any(x in inst_raw for x in ("/", "\\", ":")):
            inst = Path(os.path.abspath(str(p)))
        else:
            inst = (_instances_root() / inst_raw).resolve()
        _load_dotenv_frozen()
        _inst_env = inst / ".env"
        if _inst_env.is_file():
            from dotenv import load_dotenv as _ld
            _ld(dotenv_path=str(_inst_env), override=True)
            _normalize_llm_env()
        from gateway_server import serve_gateway
        serve_gateway(host=str(args.host), port=int(args.port), instance_dir=inst)
        return 0
    func = getattr(args, "func", None)
    if func is None:
        inst_raw = (args.instance_dir or "").strip()
        if inst_raw:
            p = Path(os.path.expanduser(inst_raw))
            if p.is_absolute() or p.exists() or any(x in inst_raw for x in ("/", "\\", ":")):
                inst = Path(os.path.abspath(str(p)))
            else:
                inst = (_instances_root() / inst_raw).resolve()
            os.environ["INSTANCE_DIR"] = str(inst)
        if not (os.getenv("LLM_API_KEY") or "").strip():
            ensure_llm_configured()
        if not (os.getenv("INSTANCE_DIR") or "").strip():
            if os.getenv("LLM_API_KEY"):
                print("配置已完成，请用 -I 指定实例启动：")
                print()
                print("  nanoghost -I <实例名>        # CLI 交互模式")
                print("  nanoghost gateway start -I <实例名>  # 守护进程")
                print()
                print("或在 dist/NanoGhost 目录下创建快捷方式，目标设为：")
                print("  NanoGhost.exe -I <实例名>")
            return 0
        auto_check_and_notify()
        _run_agent_mode()
        return 0
    return int(func(args) or 0)


def _run_agent_mode() -> None:
    import asyncio
    import logging

    # Ensure SSL certs are available in PyInstaller frozen environment
    try:
        import certifi as _certifi
        _cert_path = _certifi.where()
        if os.path.isfile(_cert_path):
            os.environ["SSL_CERT_FILE"] = _cert_path
    except Exception:
        pass
    if not os.environ.get("SSL_CERT_FILE"):
        if getattr(sys, "frozen", False):
            _bundled = os.path.join(sys._MEIPASS, "certifi", "cacert.pem")
            if os.path.isfile(_bundled):
                os.environ["SSL_CERT_FILE"] = _bundled

    # Trigger run.py module-level bootstrap (env loading, log setup, etc.)
    import run  # noqa: F401

    from agent_core import Agent, AgentConfig
    from agent_core.adapters import SqliteDatabase, OpenAILLM, SqliteImagePort
    from run import assemble_sys_prompt, run_cli_chat

    inst_dir = os.getenv("INSTANCE_DIR", "").strip()
    if not inst_dir:
        logging.getLogger("agent_core").error("未指定实例目录。请先创建实例后使用 -I 启动:\n"
                                              "  nanoghost instance create <名称>\n"
                                              "  nanoghost -I <实例名>")
        return
    data_dir = os.path.join(inst_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "agent_data.db")

    mode = os.getenv("AGENT_MODE", "cli").lower()
    if mode == "feishu":
        if not os.getenv("FEISHU_APP_ID") or not os.getenv("FEISHU_APP_SECRET"):
            logging.getLogger("agent_core").error("飞书模式需要设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
            return
        db = SqliteDatabase(db_path=db_path)
        llm = OpenAILLM()
        image_port = SqliteImagePort(db)
        namespace = os.getenv("AGENT_NAMESPACE", "").strip() or "feishu-agent"
        # 技能是实例级的，这条入口也得把实例技能目录带上（和 run.py 的 run_feishu 一致）
        from agent_core.skill.discovery import resolve_instance_skill_dirs
        agent = Agent(db=db, llm=llm, image_port=image_port, namespace=namespace,
                      skill_extra_dirs=resolve_instance_skill_dirs() or None)
        sys_prompt = assemble_sys_prompt()
        logging.getLogger("agent_core").info("System prompt 长度: %s 字", len(sys_prompt))
        from agent_core.channel.feishu import FeishuWSClient
        ws_client = FeishuWSClient(
            agent=agent,
            sys_prompt=sys_prompt,
            api_spec={},
            base_url=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        )
        logging.getLogger("agent_core").info("Agent 启动完毕, 等待飞书消息...")
        asyncio.run(ws_client.run_forever())
    else:
        run_cli_chat()


if __name__ == "__main__":
    _frozen = getattr(sys, "frozen", False)
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit as e:
        if _frozen and e.code != 0:
            try:
                input("\n按任意键退出...")
            except Exception:
                pass
        raise
