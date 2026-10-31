import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from postgrest.exceptions import APIError

from ..cover_image import build_cover_image_url
from ..schemas import (
    AGE_RANGES,
    CATEGORY_SLUGS,
    Story,
    StoryCreate,
    StoryList,
    StorySummary,
    StoryUpdate,
)
from ..security import require_api_key
from ..supabase_client import admin_client, public_client

router = APIRouter(prefix="/stories", tags=["stories"])

# Everything except `body` — see StorySummary's docstring for why the listing withholds it.
SUMMARY_COLUMNS = (
    "slug, title, emoji, excerpt, category, age_range, read_time_minutes, "
    "tags, cover_image_url, published_at"
)

UNIQUE_VIOLATION = "23505"


def slugify(title: str) -> str:
    """Port of slugify() in shared/utils/story-authoring.ts, so slugs match /studio's."""
    base = re.sub(r"['\"]", "", title.lower())
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:60]
    return base or "new-story"


def _serialize(payload: dict[str, Any]) -> dict[str, Any]:
    """PostgREST takes JSON, so datetimes have to go over the wire as ISO strings."""
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in payload.items()
    }


def _fetch_row(slug: str, columns: str = "*") -> dict[str, Any] | None:
    response = public_client().table("stories").select(columns).eq("slug", slug).limit(1).execute()
    rows = response.data or []
    return rows[0] if rows else None


@router.get("", response_model=StoryList)
async def list_stories(
    category: str | None = Query(default=None, description="Filter by category slug."),
    age_range: str | None = Query(default=None, alias="ageRange", description="Filter by age band."),
    tag: str | None = Query(default=None, description="Only stories carrying this tag."),
    q: str | None = Query(default=None, description="Case-insensitive search over title and excerpt."),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> StoryList:
    """Newest-first listing of stories, without their body text."""
    if category and category not in CATEGORY_SLUGS:
        raise HTTPException(status_code=400, detail=f"Unknown category '{category}'.")
    if age_range and age_range not in AGE_RANGES:
        raise HTTPException(status_code=400, detail=f"Unknown age range '{age_range}'.")

    query = public_client().table("stories").select(SUMMARY_COLUMNS, count="exact")
    if category:
        query = query.eq("category", category)
    if age_range:
        query = query.eq("age_range", age_range)
    if tag:
        query = query.contains("tags", [tag])
    if q and q.strip():
        # Quote the term so commas and spaces can't break out of PostgREST's
        # comma-separated or() syntax; backslashes and quotes are stripped first.
        term = re.sub(r'[\\"]', "", q.strip())
        query = query.or_(f'title.ilike."*{term}*",excerpt.ilike."*{term}*"')

    response = query.order("published_at", desc=True).range(offset, offset + limit - 1).execute()
    items = [StorySummary.model_validate(row) for row in (response.data or [])]
    total = response.count if response.count is not None else len(items)
    return StoryList(total=total, limit=limit, offset=offset, items=items)


@router.get("/{slug}", response_model=Story)
async def get_story(slug: str) -> Story:
    """A single story, in full. Every story is free to read."""
    row = _fetch_row(slug)
    if not row:
        raise HTTPException(status_code=404, detail="Story not found.")
    return Story.model_validate(row)


@router.post("", response_model=Story, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_api_key)])
async def create_story(payload: StoryCreate) -> Story:
    """Creates a story. Slug and cover image are derived from the title unless supplied."""
    return insert_story(payload)


def insert_story(payload: StoryCreate, *, created_by: str | None = None) -> Story:
    """Inserts a story, deriving the slug and cover; shared with the partner route.

    `created_by` names the partner app a story came from (the `created_by`
    column); the owner's own stories leave it out.
    """
    explicit_slug = bool(payload.slug and payload.slug.strip())
    base_slug = slugify(payload.slug) if explicit_slug else slugify(payload.title)
    published_at = payload.published_at or datetime.now(timezone.utc)

    record = {
        "title": payload.title.strip(),
        "emoji": payload.emoji.strip() or "✨",
        "excerpt": payload.excerpt.strip(),
        "category": payload.category,
        "age_range": payload.age_range,
        "read_time_minutes": payload.read_time_minutes,
        "tags": payload.tags,
        "body": payload.body,
        "published_at": published_at,
    }
    if created_by:
        record["created_by"] = created_by

    # Retry on slug collision the same way publishStoryDraft() does — but only when
    # the slug was derived. An explicit slug that's taken is the caller's mistake.
    attempts = 1 if explicit_slug else 20
    slug = base_slug
    for attempt in range(1, attempts + 1):
        candidate = dict(record, slug=slug)
        candidate["cover_image_url"] = payload.cover_image_url or build_cover_image_url(
            payload.title, payload.category, slug
        )
        try:
            response = admin_client().table("stories").insert(_serialize(candidate)).execute()
        except APIError as exc:
            if exc.code == UNIQUE_VIOLATION:
                if explicit_slug:
                    raise HTTPException(status_code=409, detail=f"Slug '{slug}' is already taken.") from exc
                slug = f"{base_slug}-{attempt + 1}"
                continue
            raise HTTPException(status_code=500, detail=exc.message or str(exc)) from exc

        return Story.model_validate((response.data or [candidate])[0])

    raise HTTPException(status_code=500, detail="Could not generate a unique slug.")


@router.patch("/{slug}", response_model=Story, dependencies=[Depends(require_api_key)])
async def update_story(slug: str, payload: StoryUpdate) -> Story:
    """Partial update — only the fields present in the request body are written."""
    changes = payload.model_dump(exclude_unset=True, by_alias=False)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update.")

    try:
        response = admin_client().table("stories").update(_serialize(changes)).eq("slug", slug).execute()
    except APIError as exc:
        raise HTTPException(status_code=500, detail=exc.message or str(exc)) from exc

    rows = response.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Story not found.")
    return Story.model_validate(rows[0])


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_api_key)])
async def delete_story(slug: str) -> Response:
    try:
        response = admin_client().table("stories").delete().eq("slug", slug).execute()
    except APIError as exc:
        raise HTTPException(status_code=500, detail=exc.message or str(exc)) from exc

    if not (response.data or []):
        raise HTTPException(status_code=404, detail="Story not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
