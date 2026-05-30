import json
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .config import MCPServerConfig


class MCPStdioClient:
    def __init__(self, cfg: MCPServerConfig):
        self.cfg = cfg
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._pending: Dict[str, threading.Event] = {}
        self._pending_results: Dict[str, Any] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self._connected = False

    def _ensure_connected(self) -> Tuple[bool, Optional[str]]:
        if self._connected and self._process and self._process.poll() is None:
            return True, None

        with self._lock:
            if self._connected and self._process and self._process.poll() is None:
                return True, None

            command = self.cfg.url
            args = list(self.cfg.extra_args or [])
            timeout = max(1, int(self.cfg.timeout_seconds))

            try:
                import io
                _popen_kw = dict(
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    encoding='utf-8',
                )
                if hasattr(subprocess, 'CREATE_NO_WINDOW'):
                    _popen_kw['creationflags'] = subprocess.CREATE_NO_WINDOW
                import os as _os
                self._process = subprocess.Popen(
                    [command] + args,
                    **_popen_kw,
                )
                import time
                time.sleep(0.5)
                if self._process.poll() is not None:
                    self._connected = False
                    return False, f'process exited immediately (code={self._process.returncode})'
            except Exception as e:
                self._connected = False
                return False, str(e)

            self._connected = True
            self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._reader_thread.start()

        # lock must be released before _initialize/_send_jsonrpc
        # or _send_jsonrpc will deadlock trying to acquire it
        ok, err = self._initialize(timeout=timeout)
        if not ok:
            with self._lock:
                self._connected = False
            self._cleanup()
            return False, err

        return True, None

    def _initialize(self, timeout: int = 10) -> Tuple[bool, Optional[str]]:
        init_params = {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "nanoghost", "version": "0.1.0"},
        }
        ok, result, err, _dur = self._send_jsonrpc("initialize", init_params, timeout=timeout)
        return ok, err

    def _cleanup(self) -> None:
        proc = self._process
        self._process = None
        self._connected = False
        if proc:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _read_loop(self) -> None:
        if not self._process or not self._process.stdout:
            return
        try:
            for line in self._process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                rid = msg.get("id")
                if rid and isinstance(rid, str):
                    with self._lock:
                        self._pending_results[rid] = msg
                        event = self._pending.pop(rid, None)
                    if event:
                        event.set()
        except Exception:
            pass
        finally:
            self._connected = False

    def _send_jsonrpc(self, method: str, params: Dict[str, Any],
                      timeout: int = 30) -> Tuple[bool, Any, Optional[str], int]:
        t0 = time.time()
        ok, err = self._ensure_connected()
        if not ok:
            return False, None, err, int((time.time() - t0) * 1000)

        if not self._process or not self._process.stdin:
            return False, None, "process not available", int((time.time() - t0) * 1000)

        self._request_id += 1
        rid = f"r{self._request_id}_{uuid.uuid4().hex[:8]}"
        event = threading.Event()
        with self._lock:
            self._pending[rid] = event

        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        try:
            self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._process.stdin.flush()
        except BrokenPipeError:
            with self._lock:
                self._pending.pop(rid, None)
            self._connected = False
            return False, None, "pipe broken", int((time.time() - t0) * 1000)
        except Exception as e:
            with self._lock:
                self._pending.pop(rid, None)
            self._connected = False
            return False, None, str(e), int((time.time() - t0) * 1000)

        if event.wait(timeout=max(1, timeout)):
            with self._lock:
                result = self._pending_results.pop(rid, None)
            if result is None:
                return False, None, "no response", int((time.time() - t0) * 1000)
            if result.get("error"):
                err_obj = result.get("error")
                if isinstance(err_obj, dict):
                    msg = err_obj.get("message") or json.dumps(err_obj, ensure_ascii=False)
                else:
                    msg = str(err_obj)
                return False, result, msg, int((time.time() - t0) * 1000)
            return True, result.get("result"), None, int((time.time() - t0) * 1000)

        with self._lock:
            self._pending.pop(rid, None)
        return False, None, "timeout", int((time.time() - t0) * 1000)

    def probe(self):
        from .http_sse import MCPProbeResult

        t0 = time.time()
        ok, err = self._ensure_connected()
        dur = int((time.time() - t0) * 1000)
        if ok:
            return MCPProbeResult(ok=True, status="connected", duration_ms=dur)
        return MCPProbeResult(ok=False, status="unreachable", error=err, duration_ms=dur)

    def list_tools(self) -> Tuple[bool, Any, Optional[str], int]:
        return self._send_jsonrpc("tools/list", {}, timeout=max(1, int(self.cfg.timeout_seconds)))

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Tuple[bool, Any, Optional[str], int]:
        return self._send_jsonrpc("tools/call", {"name": name, "arguments": arguments or {}},
                                   timeout=max(1, int(self.cfg.timeout_seconds)))
