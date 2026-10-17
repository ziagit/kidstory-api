import secrets
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings, get_settings
from .mcp_server import build_mcp_server, build_partner_mcp_server
from .partners import bearer_token, find_partner
from .routers import agents, categories, health, partner, stories

API_DESCRIPTION = (
    "REST API for the Kidstory story catalog. Reads are public; creating, "
    "updating and deleting stories requires the `X-API-Key` header, as do the "
    "`/agents/*` endpoints, which run the story and social-media agents: every "
    "story is written by the story agent. Partner apps create stories with "
    "their own key through `POST /partner/stories` or the `/partner/mcp` server. "
    "Premium stories are redacted server-side unless the caller sends a Supabase "
    "access token belonging to a reader who has purchased full access."
)


class BearerTokenGate:
    """Requires `Authorization: Bearer <token>` on everything routed through it.

    Wraps the mounted MCP app: MCP clients send the header on every request, and
    a constant-time comparison keeps the token from leaking through timing.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
            presented = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""
            if not presented or not secrets.compare_digest(presented, self.token):
                response = JSONResponse({"detail": "Invalid or missing bearer token."}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class PartnerKeyGate:
    """Requires a valid, active partner key (`Authorization: Bearer <key>`) on /partner/mcp."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
            try:
                partner = find_partner(bearer_token(header))
            except HTTPException as exc:
                partner, refusal = None, JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            else:
                refusal = JSONResponse({"detail": "Invalid, revoked or missing partner key."}, status_code=401)
            if partner is None:
                await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    """DNS-rebinding protection for /mcp, keyed on the hostnames we're served under.

    With no PUBLIC_HOSTS configured the check is off — the alternative, a
    loopback-only allowlist, would reject every request that arrives through a
    tunnel or a hosting platform's domain.
    """
    hosts = settings.public_host_list()
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    local = ["127.0.0.1", "localhost", "[::1]"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*(f"{h}:*" for h in [*hosts, *local]), *hosts],
        allowed_origins=[*(f"https://{h}" for h in hosts), *(f"http://{h}:*" for h in local)],
    )


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The Starlette app streamable_http_app() returns defines its own lifespan,
        # but mounting it doesn't propagate that automatically — it has to be
        # entered here so the session manager's task group is actually running.
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(app.state.mcp_server.session_manager.run())
            await stack.enter_async_context(app.state.partner_mcp_server.session_manager.run())
            yield

    app = FastAPI(title="Kidstory API", description=API_DESCRIPTION, version="0.2.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(categories.router)
    app.include_router(stories.router)
    app.include_router(agents.router)
    app.include_router(partner.router)

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, str]:
        return {
            "name": "Kidstory API",
            "docs": "/docs",
            "health": "/health",
            "mcp": "/mcp",
            "partnerMcp": "/partner/mcp",
        }

    # Write and generate tools exist behind a bearer token, or — explicit opt-in —
    # on the open endpoint. See mcp_server.py and MCP_PUBLIC_WRITES in config.py.
    include_writes = bool(settings.mcp_bearer_token) or settings.mcp_public_writes
    mcp_server = build_mcp_server(app, include_writes=include_writes)
    app.state.mcp_server = mcp_server
    # host="0.0.0.0" opts out of the loopback-only default; the real host check
    # is the explicit transport_security built from PUBLIC_HOSTS.
    mcp_app: ASGIApp = mcp_server.streamable_http_app(
        host="0.0.0.0",
        transport_security=_transport_security(settings),
        stateless_http=settings.mcp_stateless,
        json_response=settings.mcp_stateless,
    )
    if settings.mcp_bearer_token:
        mcp_app = BearerTokenGate(mcp_app, settings.mcp_bearer_token)

    # Partner apps get their own MCP server at /partner/mcp, behind their keys.
    partner_mcp_server = build_partner_mcp_server(app)
    app.state.partner_mcp_server = partner_mcp_server
    partner_mcp_app = partner_mcp_server.streamable_http_app(
        host="0.0.0.0",
        transport_security=_transport_security(settings),
        stateless_http=settings.mcp_stateless,
        json_response=settings.mcp_stateless,
    )
    # Mounted after the routers, so POST /partner/stories still reaches its route.
    app.mount("/partner", PartnerKeyGate(partner_mcp_app))

    # Mounted last (and at root) so it only catches paths — like /mcp — that don't
    # match one of the explicit routes registered above.
    app.mount("/", mcp_app)
    return app


app = create_app()
