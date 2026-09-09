from fastapi import APIRouter

from ..schemas import Health
from ..supabase_client import public_client

router = APIRouter(tags=["meta"])


@router.get("/health", response_model=Health)
async def health() -> Health:
    """Liveness plus a cheap round-trip to Supabase, so a bad key shows up here."""
    try:
        public_client().table("stories").select("slug").limit(1).execute()
        database = "ok"
    except Exception as exc:  # noqa: BLE001 - health must report, never raise
        database = f"error: {exc}"
    return Health(status="ok", database=database)
