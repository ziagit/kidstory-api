from fastapi import APIRouter, HTTPException, Query
from postgrest.exceptions import APIError

from ..schemas import BookDigest, BookDigestList
from ..supabase_client import public_client

router = APIRouter(prefix="/books", tags=["books"])

SUMMARY_COLUMNS = "slug, title, emoji, excerpt, cover_image_url, published_at"


def _not_set_up(exc: APIError) -> HTTPException:
    # The book_digests table comes from supabase/migrations/004_book_digests.sql.
    return HTTPException(status_code=503, detail=f"Book Picks isn't set up on this server: {exc.message or exc}")


@router.get("", response_model=BookDigestList)
async def list_digests(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> BookDigestList:
    """Newest-first Book Picks digests (written by the books agent), without their text."""
    try:
        response = (
            public_client()
            .table("book_digests")
            .select(SUMMARY_COLUMNS, count="exact")
            .order("published_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
    except APIError as exc:
        raise _not_set_up(exc) from exc
    rows = response.data or []
    return BookDigestList(total=response.count or len(rows), limit=limit, offset=offset, items=rows)


@router.get("/{slug}", response_model=BookDigest)
async def get_digest(slug: str) -> BookDigest:
    """One Book Picks digest, with its text, the books it names and its sources. Always free to read."""
    try:
        rows = public_client().table("book_digests").select("*").eq("slug", slug).limit(1).execute().data or []
    except APIError as exc:
        raise _not_set_up(exc) from exc
    if not rows:
        raise HTTPException(status_code=404, detail="Digest not found.")
    return BookDigest.model_validate(rows[0])
