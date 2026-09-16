"""MCP server exposing the Kidstory catalog as tools for MCP-compatible clients (e.g. ChatGPT Connectors).

Tools are thin wrappers around this same API's own HTTP endpoints, so there's
exactly one place — the routers — that implements the actual business logic,
validation and error handling. The calls go through an in-process ASGI
transport rather than the network, so they work regardless of which port or
host the server happens to be bound to.

The catalog-reading tools are always registered. The write and generate tools
spend the server's own KIDSTORY_API_KEY and its model compute, so they exist
only when /mcp itself is protected by MCP_BEARER_TOKEN (see main.py) — an open
/mcp is browse-only by construction.
"""

from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from starlette.types import ASGIApp

from .config import get_settings

READ_ONLY_INSTRUCTIONS = (
    "Tools for browsing the Kidstory children's story catalog. This endpoint is "
    "read-only: stories can be listed and read, not changed."
)
FULL_INSTRUCTIONS = (
    "Tools for browsing and managing the Kidstory children's story catalog. "
    "Reads are always available. `generate_story` drafts a story with Kidstory's "
    "own model without saving it. Writes (create/update/delete) modify the live "
    "production catalog immediately — confirm with the user before calling them."
)


def build_mcp_server(app: ASGIApp, *, include_writes: bool) -> MCPServer:
    """A fresh MCPServer whose tools call `app` in-process.

    Built per application instance (not at import) because the streamable-HTTP
    session manager it owns can only be started once, and the test suite spins
    up many apps.
    """
    server = MCPServer(
        name="kidstory-api",
        instructions=FULL_INSTRUCTIONS if include_writes else READ_ONLY_INSTRUCTIONS,
    )

    def _write_headers() -> dict[str, str]:
        api_key = get_settings().kidstory_api_key
        if not api_key:
            raise RuntimeError("Write access is not configured on this server (KIDSTORY_API_KEY is unset).")
        return {"X-API-Key": api_key}

    async def _request(method: str, path: str, **kwargs: Any) -> Any:
        transport = httpx.ASGITransport(app=app)
        # Generation can take a couple of minutes on the free CPU tier.
        async with httpx.AsyncClient(transport=transport, base_url="http://kidstory-api", timeout=180.0) as client:
            response = await client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise RuntimeError(f"{method} {path} failed ({response.status_code}): {detail}")
        if response.status_code == 204:
            return {"status": "deleted"}
        return response.json()

    @server.tool()
    async def list_categories() -> Any:
        """List the fixed set of story categories (slug + display label)."""
        return await _request("GET", "/categories")

    @server.tool()
    async def list_stories(
        category: str | None = None,
        age_range: str | None = None,
        tag: str | None = None,
        q: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Any:
        """List stories, newest first (no body text). Filter by category, age_range, tag, or a text search `q`."""
        params = {
            "category": category,
            "ageRange": age_range,
            "tag": tag,
            "q": q,
            "limit": limit,
            "offset": offset,
        }
        params = {key: value for key, value in params.items() if value is not None}
        return await _request("GET", "/stories", params=params)

    @server.tool()
    async def get_story(slug: str) -> Any:
        """Get one story by slug. Premium stories are redacted to their opening paragraph."""
        return await _request("GET", f"/stories/{slug}")

    if not include_writes:
        return server

    @server.tool()
    async def generate_story(
        topic: str,
        category: str | None = None,
        age_range: str | None = None,
    ) -> Any:
        """Draft a new story about `topic` with Kidstory's own story model. Returns the draft without saving it; pass it to create_story to publish."""
        payload = {"topic": topic, "category": category, "ageRange": age_range}
        payload = {key: value for key, value in payload.items() if value is not None}
        return await _request("POST", "/stories/generate", json=payload, headers=_write_headers())

    @server.tool()
    async def create_story(
        title: str,
        excerpt: str,
        body: list[str],
        category: str = "adventure",
        age_range: str = "all-ages",
        emoji: str = "✨",
        read_time_minutes: int = 2,
        tags: list[str] | None = None,
        is_premium: bool = True,
        slug: str | None = None,
    ) -> Any:
        """Create a new story. Slug and cover image are derived automatically unless given."""
        payload = {
            "title": title,
            "excerpt": excerpt,
            "body": body,
            "category": category,
            "age_range": age_range,
            "emoji": emoji,
            "read_time_minutes": read_time_minutes,
            "tags": tags or [],
            "is_premium": is_premium,
            "slug": slug,
        }
        return await _request("POST", "/stories", json=payload, headers=_write_headers())

    @server.tool()
    async def update_story(
        slug: str,
        title: str | None = None,
        excerpt: str | None = None,
        body: list[str] | None = None,
        category: str | None = None,
        age_range: str | None = None,
        emoji: str | None = None,
        read_time_minutes: int | None = None,
        tags: list[str] | None = None,
        is_premium: bool | None = None,
    ) -> Any:
        """Partially update a story by slug. Only the fields you pass are changed."""
        payload = {
            "title": title,
            "excerpt": excerpt,
            "body": body,
            "category": category,
            "age_range": age_range,
            "emoji": emoji,
            "read_time_minutes": read_time_minutes,
            "tags": tags,
            "is_premium": is_premium,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        if not payload:
            raise RuntimeError("No fields to update — pass at least one field besides slug.")
        return await _request("PATCH", f"/stories/{slug}", json=payload, headers=_write_headers())

    @server.tool()
    async def delete_story(slug: str) -> Any:
        """Permanently delete a story by slug."""
        return await _request("DELETE", f"/stories/{slug}", headers=_write_headers())

    return server
