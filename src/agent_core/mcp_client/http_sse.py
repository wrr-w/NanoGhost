import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from mcp import ClientSession
from mcp.client.sse import sse_client

from .config import MCPServerConfig


@dataclass
class MCPProbeResult:
    ok: bool
    status: str
    error: Optional[str] = None
    duration_ms: int = 0


def _dump_model(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, mode="json", exclude_none=True)
    return obj


async def _run_session(url: str, headers: dict, timeout: int, op):
    async with sse_client(url, headers=headers, timeout=timeout) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await op(session)


def _run_in_new_loop(coro_factory):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro_factory())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


class MCPHttpSSEClient:
    def __init__(self, cfg: MCPServerConfig):
        self.cfg = cfg

    def _run(self, op):
        headers = dict(self.cfg.headers or {})
        timeout = max(1, int(self.cfg.timeout_seconds))
        url = self.cfg.url
        return _run_in_new_loop(lambda: _run_session(url, headers, timeout, op))

    def probe(self) -> MCPProbeResult:
        t0 = time.time()
        try:
            self._run(lambda session: asyncio.sleep(0, result=True))
            return MCPProbeResult(ok=True, status="connected", duration_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            return MCPProbeResult(ok=False, status="unreachable", error=str(e), duration_ms=int((time.time() - t0) * 1000))

    def list_tools(self) -> Tuple[bool, Any, Optional[str], int]:
        t0 = time.time()
        try:
            result = self._run(lambda session: session.list_tools())
            return True, _dump_model(result), None, int((time.time() - t0) * 1000)
        except Exception as e:
            return False, None, str(e), int((time.time() - t0) * 1000)

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Tuple[bool, Any, Optional[str], int]:
        t0 = time.time()
        try:
            result = self._run(lambda session: session.call_tool(name, arguments or {}))
            return True, _dump_model(result), None, int((time.time() - t0) * 1000)
        except Exception as e:
            return False, None, str(e), int((time.time() - t0) * 1000)
