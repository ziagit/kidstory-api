"""One shared outbound HTTP client, created at startup and closed at shutdown.

Kept in app state rather than a module global so tests can swap in a client
backed by httpx.MockTransport without any real network access.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.http_client = httpx.AsyncClient(follow_redirects=True)
    try:
        yield
    finally:
        await app.state.http_client.aclose()


async def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client
