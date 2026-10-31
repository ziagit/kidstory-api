from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from postgrest.exceptions import APIError
from pydantic.alias_generators import to_camel

from .. import visitors
from ..security import require_api_key


def _camel(value: Any) -> Any:
    """camelCase keys all the way down, like the API's other responses."""
    if isinstance(value, dict):
        return {to_camel(k): _camel(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_camel(v) for v in value]
    return value


router = APIRouter(prefix="/visitors", tags=["visitors"], dependencies=[Depends(require_api_key)])


@router.get("/stats")
async def visitor_stats(days: int = Query(default=30, ge=1, le=90)) -> dict:
    """Visitors and time on the site for the last `days` days (Pakistan time), with all-time totals.

    Owner-only: the admin's Visitors page shows it, and the visitors agent reads
    the same numbers. Times are in seconds; `liveNow` is visitors seen in the
    last two minutes.
    """
    try:
        return _camel(visitors.visitor_stats(days))
    except APIError as exc:
        # The site_visits table comes from the Nuxt app's supabase/migrations/006_site_visits.sql.
        raise HTTPException(status_code=503, detail=f"Visitor tracking isn't set up on this server: {exc.message or exc}") from exc
