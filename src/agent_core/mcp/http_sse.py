import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin

import requests

from .config import MCPServerConfig


@dataclass
class MCPProbeResult:
    ok: bool
    status: str
    error: Optional[str] = None
    duration_ms: int = 0


class MCPHttpSSEClient:
    def __init__(self, cfg: MCPServerConfig):
        self.cfg = cfg
        self._message_url: Optional[str] = None

    def _headers(self) -> Dict[str, str]:
        return dict(self.cfg.headers or {})

    def _sse_url(self) -> str:
        return urljoin(self.cfg.url.rstrip("/") + "/", "sse")

    def _default_messages_url(self) -> str:
        return urljoin(self.cfg.url.rstrip("/") + "/", "messages")

    def _ensure_message_url(self) -> Tuple[bool, Optional[str]]:
        if self._message_url:
            return True, self._message_url

        start = time.time()
        try:
            r = requests.get(
                self._sse_url(),
                headers={**self._headers(), "Accept": "text/event-stream"},
                stream=True,
                timeout=(5, max(1, int(self.cfg.timeout_seconds))),
            )
        except Exception as e:
            return False, str(e)

        endpoint: Optional[str] = None
        data_lines = []

        try:
            for raw in r.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                    continue
                if line == "":
                    if not data_lines:
                        continue
                    data_str = "\n".join(data_lines).strip()
                    data_lines.clear()
                    try:
                        payload = json.loads(data_str)
                    except Exception:
                        payload = None
                    if isinstance(payload, dict) and isinstance(payload.get("endpoint"), str):
                        endpoint = payload["endpoint"].strip()
                        break
                if time.time() - start > max(1, int(self.cfg.timeout_seconds)):
                    break
        finally:
            try:
                r.close()
            except Exception:
                pass

        if endpoint:
            self._message_url = urljoin(self.cfg.url.rstrip("/") + "/", endpoint.lstrip("/"))
            return True, self._message_url

        self._message_url = self._default_messages_url()
        return True, self._message_url

    def probe(self) -> MCPProbeResult:
        t0 = time.time()
        ok, err = self._ensure_message_url()
        duration_ms = int((time.time() - t0) * 1000)
        if ok:
            return MCPProbeResult(ok=True, status="connected", duration_ms=duration_ms)
        return MCPProbeResult(ok=False, status="unreachable", error=err, duration_ms=duration_ms)

    def _post_jsonrpc(self, method: str, params: Dict[str, Any]) -> Tuple[bool, Any, Optional[str], int]:
        t0 = time.time()
        ok, err = self._ensure_message_url()
        if not ok:
            return False, None, err, int((time.time() - t0) * 1000)
        assert self._message_url

        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": params or {},
        }
        try:
            r = requests.post(
                self._message_url,
                headers={**self._headers(), "Content-Type": "application/json"},
                json=payload,
                timeout=max(1, int(self.cfg.timeout_seconds)),
            )
            data = r.json() if r.text else {}
        except Exception as e:
            return False, None, str(e), int((time.time() - t0) * 1000)

        if isinstance(data, dict) and data.get("error"):
            err_obj = data.get("error")
            if isinstance(err_obj, dict):
                msg = err_obj.get("message") or json.dumps(err_obj, ensure_ascii=False)
            else:
                msg = str(err_obj)
            return False, data, msg, int((time.time() - t0) * 1000)
        if not isinstance(data, dict):
            return False, data, "invalid response", int((time.time() - t0) * 1000)
        return True, data.get("result"), None, int((time.time() - t0) * 1000)

    def list_tools(self) -> Tuple[bool, Any, Optional[str], int]:
        return self._post_jsonrpc("tools/list", {})

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Tuple[bool, Any, Optional[str], int]:
        return self._post_jsonrpc("tools/call", {"name": name, "arguments": arguments or {}})

