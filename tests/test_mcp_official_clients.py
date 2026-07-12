from agent_core.mcp_client.config import MCPServerConfig
from agent_core.mcp_client.http_sse import MCPHttpSSEClient
from agent_core.mcp_client.stdio_client import MCPStdioClient


class _Dumpable:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, **kwargs):
        return self.payload


def _sse_cfg():
    return MCPServerConfig(
        server_id="iwms",
        transport="sse",
        url="http://127.0.0.1:8001/mcp/sse",
        headers={},
        timeout_seconds=5,
    )


def _stdio_cfg():
    return MCPServerConfig(
        server_id="capture",
        transport="stdio",
        url="python",
        headers={},
        timeout_seconds=5,
        extra_args=["-V"],
    )


def test_http_sse_client_exposes_existing_sync_contract():
    client = MCPHttpSSEClient(_sse_cfg())
    assert hasattr(client, "probe")
    assert hasattr(client, "list_tools")
    assert hasattr(client, "call_tool")


def test_stdio_client_exposes_existing_sync_contract():
    client = MCPStdioClient(_stdio_cfg())
    assert hasattr(client, "probe")
    assert hasattr(client, "list_tools")
    assert hasattr(client, "call_tool")


def test_http_sse_client_uses_dumped_tool_payload(monkeypatch):
    client = MCPHttpSSEClient(_sse_cfg())

    async def _fake_run_session(op):
        class _Session:
            async def list_tools(self):
                return _Dumpable({"tools": [{"name": "iwms_query"}]})

        return await op(_Session())

    monkeypatch.setattr(client, "_run_session", _fake_run_session)
    ok, result, err, _dur = client.list_tools()

    assert ok is True
    assert err is None
    assert result == {"tools": [{"name": "iwms_query"}]}


def test_stdio_client_probe_returns_result_object(monkeypatch):
    client = MCPStdioClient(_stdio_cfg())

    async def _fake_run_session(op):
        class _Session:
            pass

        return await op(_Session())

    monkeypatch.setattr(client, "_run_session", _fake_run_session)
    result = client.probe()

    assert result.ok is True
    assert result.status == "connected"
