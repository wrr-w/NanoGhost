from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from agent_core.utils.process import pid_exists, terminate_pid

# 渠道注册表：{channel_name: {enabled: bool, worker_class: str}}
CHANNEL_REGISTRY = {
    "feishu": {"enabled": False, "worker_key": "feishu"},
    "cli": {"enabled": True},
}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def instance_dir_from_env() -> Path:
    inst = (os.getenv("INSTANCE_DIR") or "").strip()
    if not inst:
        raise RuntimeError("INSTANCE_DIR is empty. Start gateway with -I/--instance-dir.")
    return Path(os.path.abspath(os.path.expanduser(inst)))


def channel_config_path(instance_dir: Path) -> Path:
    return instance_dir / "channel_directory.json"


def default_channel_config() -> dict[str, Any]:
    channels = {}
    for name, cfg in CHANNEL_REGISTRY.items():
        channels[name] = {"enabled": cfg.get("enabled", False)}
    return {
        "updated_at": None,
        "channels": channels,
    }


def load_channel_config(instance_dir: Path) -> dict[str, Any]:
    path = channel_config_path(instance_dir)
    raw = _read_json(path)
    if not raw:
        return default_channel_config()
    raw.setdefault("updated_at", None)
    ch = raw.get("channels")
    if not isinstance(ch, dict):
        raw["channels"] = {}
    for name, cfg in CHANNEL_REGISTRY.items():
        raw["channels"].setdefault(name, {"enabled": cfg.get("enabled", False)})
    return raw


def save_channel_config(instance_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("channels", {})
    if not isinstance(cfg.get("channels"), dict):
        cfg["channels"] = {}
    cfg["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _atomic_write_json(channel_config_path(instance_dir), cfg)
    return cfg


def _read_body_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        length = int(handler.headers.get("content-length") or 0)
    except Exception:
        length = 0
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _send_json(handler: BaseHTTPRequestHandler, code: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class GatewayState:
    def __init__(self, instance_dir: Path):
        self.instance_dir = instance_dir
        self.started_at = int(time.time())
        self.workers = WorkerManager(instance_dir)


class WorkerManager:
    def __init__(self, instance_dir: Path):
        self.instance_dir = instance_dir
        self._procs: Dict[str, Optional[subprocess.Popen]] = {}

    def _runtime_dir(self) -> Path:
        return self.instance_dir / "runtime"

    def _worker_runtime_path(self, worker_key: str) -> Path:
        return self._runtime_dir() / f"{worker_key}_worker.json"

    def _load_runtime(self, worker_key: str) -> dict[str, Any]:
        return _read_json(self._worker_runtime_path(worker_key))

    def _save_runtime(self, worker_key: str, data: dict[str, Any]) -> None:
        _atomic_write_json(self._worker_runtime_path(worker_key), data)

    def _run_py_path(self) -> Path:
        return Path(__file__).resolve().with_name("run.py")

    @staticmethod
    def _resolve_python() -> str:
        """返回应使用的 Python 解释器路径。

        优先使用项目同级的 venv/Scripts/python.exe，
        回退到当前解释器（sys.executable）。
        """
        py = sys.executable
        here = Path(__file__).resolve().parent
        venv_candidates = [
            here / "venv" / "Scripts" / "python.exe",
            here / ".venv" / "Scripts" / "python.exe",
            here / ".venv" / "bin" / "python",
            here / "venv" / "bin" / "python",
        ]
        for vp in venv_candidates:
            if vp.is_file():
                py = str(vp)
                break
        return py

    def _refresh_proc_state(self, worker_key: str) -> None:
        proc = self._procs.get(worker_key)
        if proc is None:
            return
        code = proc.poll()
        if code is None:
            return
        rt = self._load_runtime(worker_key)
        rt["running"] = False
        rt["exit_code"] = int(code)
        rt["updated_at"] = int(time.time())
        self._save_runtime(worker_key, rt)
        self._procs[worker_key] = None

    def worker_status(self, worker_key: str) -> dict[str, Any]:
        self._refresh_proc_state(worker_key)
        rt = self._load_runtime(worker_key)
        pid = int(rt.get("pid") or 0)
        running = bool(rt.get("running")) and pid_exists(pid)
        if rt and pid and not running:
            rt["running"] = False
            rt["updated_at"] = int(time.time())
            self._save_runtime(worker_key, rt)
        return {
            "running": running,
            "pid": pid or None,
            "started_at": rt.get("started_at"),
            "exit_code": rt.get("exit_code"),
            "last_error": rt.get("last_error") or "",
        }

    def start_worker(self, worker_key: str, env_overrides: Optional[Dict[str, str]] = None) -> dict[str, Any]:
        st = self.worker_status(worker_key)
        if st.get("running"):
            return {"ok": True, "already_running": True, **st}

        env = dict(os.environ)
        if env_overrides:
            env.update(env_overrides)
        env["INSTANCE_DIR"] = str(self.instance_dir)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        python = self._resolve_python()
        cmd = [python, str(self._run_py_path())]

        self._runtime_dir().mkdir(parents=True, exist_ok=True)
        try:
            p = subprocess.Popen(cmd, cwd=str(self.instance_dir), env=env)
        except Exception as e:
            rt = {"running": False, "pid": None, "started_at": None, "last_error": str(e), "updated_at": int(time.time())}
            self._save_runtime(worker_key, rt)
            return {"ok": False, "error": str(e)}

        self._procs[worker_key] = p
        rt = {
            "running": True,
            "pid": int(p.pid),
            "started_at": int(time.time()),
            "cmd": cmd,
            "updated_at": int(time.time()),
            "last_error": "",
        }
        self._save_runtime(worker_key, rt)
        return {"ok": True, "pid": int(p.pid)}

    def stop_worker(self, worker_key: str) -> dict[str, Any]:
        self._refresh_proc_state(worker_key)
        rt = self._load_runtime(worker_key)
        pid = int(rt.get("pid") or 0)
        proc = self._procs.get(worker_key)
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            self._procs[worker_key] = None
        else:
            terminate_pid(pid)
        rt["running"] = False
        rt["updated_at"] = int(time.time())
        self._save_runtime(worker_key, rt)
        return {"ok": True}

    def feishu_status(self) -> dict[str, Any]:
        return self.worker_status("feishu")

    def start_feishu(self) -> dict[str, Any]:
        return self.start_worker("feishu", env_overrides={"AGENT_MODE": "feishu"})

    def stop_feishu(self) -> dict[str, Any]:
        return self.stop_worker("feishu")

    def all_workers_status(self) -> Dict[str, Any]:
        result = {}
        for name, cfg in CHANNEL_REGISTRY.items():
            wk = cfg.get("worker_key")
            if wk:
                result[name] = {"enabled": cfg.get("enabled", False), **self.worker_status(wk)}
        return result


class GatewayHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer  # type: ignore

    def _state(self) -> GatewayState:
        return getattr(self.server, "state")

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        state = self._state()
        if self.path == "/api/health":
            cfg = load_channel_config(state.instance_dir)
            feishu = state.workers.feishu_status()
            _send_json(
                self,
                200,
                {
                    "ok": True,
                    "instance_dir": str(state.instance_dir),
                    "started_at": state.started_at,
                    "channels": cfg.get("channels", {}),
                    "workers": {"feishu": feishu},
                },
            )
            return

        if self.path == "/api/channels":
            cfg = load_channel_config(state.instance_dir)
            _send_json(
                self,
                200,
                {"ok": True, "path": str(channel_config_path(state.instance_dir)), "config": cfg},
            )
            return

        if self.path == "/api/status":
            cfg = load_channel_config(state.instance_dir)
            ch = cfg.get("channels", {}) if isinstance(cfg.get("channels"), dict) else {}
            _send_json(
                self,
                200,
                {
                    "ok": True,
                    "instance_dir": str(state.instance_dir),
                    "channels": ch,
                    "workers": state.workers.all_workers_status(),
                },
            )
            return

        _send_json(self, 404, {"ok": False, "detail": "not found"})

    def do_POST(self) -> None:
        state = self._state()
        if self.path == "/api/start":
            cfg = load_channel_config(state.instance_dir)
            ch = cfg.get("channels", {}) if isinstance(cfg.get("channels"), dict) else {}
            workers_result = {}
            for name, channel_cfg in ch.items():
                if not isinstance(channel_cfg, dict):
                    continue
                enabled = bool(channel_cfg.get("enabled"))
                reg = CHANNEL_REGISTRY.get(name, {})
                wk = reg.get("worker_key")
                if wk and enabled:
                    r = state.workers.start_worker(wk, env_overrides={"AGENT_MODE": wk})
                    workers_result[name] = {"enabled": True, **r}
                elif wk:
                    state.workers.stop_worker(wk)
                    workers_result[name] = {"enabled": False, "ok": True, "running": False}
            _send_json(self, 200, {"ok": True, "workers": workers_result})
            return

        if self.path == "/api/stop":
            r = state.workers.stop_feishu()
            _send_json(self, 200, {"ok": True, "workers": {"feishu": r}})
            return

        if self.path == "/api/restart":
            cfg = load_channel_config(state.instance_dir)
            ch = cfg.get("channels", {}) if isinstance(cfg.get("channels"), dict) else {}
            workers_result = {}
            for name, channel_cfg in ch.items():
                if not isinstance(channel_cfg, dict):
                    continue
                enabled = bool(channel_cfg.get("enabled"))
                reg = CHANNEL_REGISTRY.get(name, {})
                wk = reg.get("worker_key")
                if wk:
                    state.workers.stop_worker(wk)
                    time.sleep(0.3)
                if wk and enabled:
                    r = state.workers.start_worker(wk, env_overrides={"AGENT_MODE": wk})
                    workers_result[name] = {"enabled": True, **r}
                elif wk:
                    workers_result[name] = {"enabled": False, "stopped": True}
            _send_json(self, 200, {"ok": True, "workers": workers_result})
            return

        _send_json(self, 404, {"ok": False, "detail": "not found"})

    def do_PUT(self) -> None:
        state = self._state()
        if self.path == "/api/channels":
            body = _read_body_json(self)
            cfg = body.get("config") if isinstance(body.get("config"), dict) else body
            saved = save_channel_config(state.instance_dir, cfg if isinstance(cfg, dict) else {})
            _send_json(self, 200, {"ok": True, "config": saved})
            return

        _send_json(self, 404, {"ok": False, "detail": "not found"})


def _install_signal_handlers(httpd: ThreadingHTTPServer) -> None:
    def _handler(_signum: int, _frame: Optional[Any]) -> None:
        try:
            httpd.shutdown()
        except Exception:
            pass

    try:
        signal.signal(signal.SIGINT, _handler)
    except Exception:
        pass
    try:
        signal.signal(signal.SIGTERM, _handler)
    except Exception:
        pass


_NANO = r"""
    .---.
   /     \
  |  o o  |
  |   ^   |
   \  -  /
    | | |
   /| | |\
  / | | | \
    | | |
    |_| |_|
"""


def _banner(host: str, port: int, instance_dir: Path) -> str:
    inst_name = instance_dir.name
    url = f"http://{host}:{port}"
    return rf"""
  {_NANO}
  +---------------------------------------------+
  |  NanoGhost Gateway                          |
  |  {url:<44} |
  |  [{inst_name}]                                 |
  +---------------------------------------------+
"""


def serve_gateway(*, host: str, port: int, instance_dir: Path) -> None:
    print(_banner(host, port, instance_dir))
    print(f"  NanoGhost gateway starting on {host}:{port}...\n")
    httpd = ThreadingHTTPServer((host, int(port)), GatewayHandler)
    setattr(httpd, "state", GatewayState(instance_dir))
    _install_signal_handlers(httpd)
    try:
        httpd.serve_forever()
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
        print(f"\n  NanoGhost gateway [{instance_dir.name}] stopped.")
