from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routers import categories, health, stories

app = FastAPI(
    title="Kidstory API",
    description=(
        "REST API for the Kidstory story catalog. Reads are public; creating, "
        "updating and deleting stories requires the `X-API-Key` header. Premium "
        "stories are redacted server-side unless the caller sends a Supabase "
        "access token belonging to a reader who has purchased full access."
    ),
    version="0.1.0",
)

settings = get_settings()
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


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {"name": "Kidstory API", "docs": "/docs", "health": "/health"}
