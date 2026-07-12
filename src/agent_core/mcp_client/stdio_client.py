import asyncio
import time
from typing import Any, Dict, Optional, Tuple

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

from .config import MCPServerConfig
from .http_sse import MCPProbeResult, _dump_model


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


async def _run_session(cfg: MCPServerConfig, op):
    params = StdioServerParameters(
        command=cfg.url,
        args=list(cfg.extra_args or []),
        env=get_default_environment(),
    )
    async with stdio_client(params) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await op(session)


class MCPStdioClient:
    def __init__(self, cfg: MCPServerConfig):
        self.cfg = cfg

    def _run(self, op):
        cfg = self.cfg
        return _run_in_new_loop(lambda: _run_session(cfg, op))

    def probe(self):
        t0 = time.time()
        try:
            self._run(lambda session: asyncio.sleep(0, result=True))
            dur = int((time.time() - t0) * 1000)
            return MCPProbeResult(ok=True, status="connected", duration_ms=dur)
        except Exception as e:
            dur = int((time.time() - t0) * 1000)
            return MCPProbeResult(ok=False, status="unreachable", error=str(e), duration_ms=dur)

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
