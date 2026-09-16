import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app

MCP_TOKEN = "mcp-secret"

# A minimal MCP initialize request — enough to get past the transport's checks.
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "tests", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _tool_names(app) -> set[str]:
    return {tool.name for tool in app.state.mcp_server._tool_manager.list_tools()}


@pytest.fixture
def mcp_env(client, monkeypatch):
    """`client` already configured Supabase + the API key; layer MCP settings on top."""

    def configure(token: str | None) -> TestClient:
        monkeypatch.setenv("MCP_BEARER_TOKEN", token or "")
        get_settings.cache_clear()
        return TestClient(create_app())

    return configure


def test_open_mcp_is_read_only(mcp_env):
    with mcp_env(None) as api:
        assert _tool_names(api.app) == {"list_categories", "list_stories", "get_story"}
        response = api.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        assert response.status_code == 200


def test_protected_mcp_registers_write_tools_and_requires_the_token(mcp_env):
    with mcp_env(MCP_TOKEN) as api:
        assert _tool_names(api.app) == {
            "list_categories",
            "list_stories",
            "get_story",
            "generate_story",
            "create_story",
            "update_story",
            "delete_story",
        }

        anonymous = api.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        assert anonymous.status_code == 401

        wrong = api.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": "Bearer nope"})
        assert wrong.status_code == 401

        ok = api.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {MCP_TOKEN}"})
        assert ok.status_code == 200

        # The gate only guards the MCP mount; the REST API stays as it was.
        assert api.get("/categories").status_code == 200


def test_public_hosts_enable_dns_rebinding_protection(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_HOSTS", "kidstory.example.com")
    get_settings.cache_clear()
    with TestClient(create_app(), base_url="http://kidstory.example.com") as api:
        assert api.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 200
        # Same app, unexpected Host header: rejected before it reaches a session.
        spoofed = api.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Host": "evil.example.net"})
        assert spoofed.status_code == 421
