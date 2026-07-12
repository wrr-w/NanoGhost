from agent_core.mcp_client.config import MCPServerConfig
from agent_core.mcp_client.http_sse import MCPHttpSSEClient


class _FakeResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            yield line

    def close(self):
        return None


def _cfg() -> MCPServerConfig:
    return MCPServerConfig(
        server_id="iwms",
        transport="sse",
        url="http://127.0.0.1:8001/mcp/sse",
        headers={},
        timeout_seconds=5,
    )


def test_sse_urls_keep_existing_sse_suffix():
    client = MCPHttpSSEClient(_cfg())

    assert client._sse_url() == "http://127.0.0.1:8001/mcp/sse"
    assert client._default_messages_url() == "http://127.0.0.1:8001/mcp/messages"


def test_ensure_message_url_accepts_plain_endpoint_event(monkeypatch):
    def fake_get(*args, **kwargs):
        return _FakeResponse(
            [
                "event: endpoint",
                "data: /mcp/messages?session_id=abc123",
                "",
            ]
        )

    monkeypatch.setattr("agent_core.mcp_client.http_sse.requests.get", fake_get)

    client = MCPHttpSSEClient(_cfg())
    ok, message_url = client._ensure_message_url()

    assert ok is True
    assert message_url == "http://127.0.0.1:8001/mcp/messages?session_id=abc123"


def test_ensure_message_url_accepts_json_endpoint_payload(monkeypatch):
    def fake_get(*args, **kwargs):
        return _FakeResponse(
            [
                "event: endpoint",
                'data: {"endpoint": "/mcp/messages?session_id=json456"}',
                "",
            ]
        )

    monkeypatch.setattr("agent_core.mcp_client.http_sse.requests.get", fake_get)

    client = MCPHttpSSEClient(_cfg())
    ok, message_url = client._ensure_message_url()

    assert ok is True
    assert message_url == "http://127.0.0.1:8001/mcp/messages?session_id=json456"
