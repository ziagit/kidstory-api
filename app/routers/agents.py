from fastapi import APIRouter, Depends, HTTPException, Query
from postgrest.exceptions import APIError

from .. import activity

from ..agents.base import AgentError
from ..agents.books_agent import run_books_agent
from ..agents.social_agent import run_social_agent
from ..agents.story_agent import run_story_agent
from ..agents.support_agent import run_support_agent
from ..schemas import (
    AgentRun,
    BooksAgentRequest,
    BooksAgentResponse,
    SocialAgentRequest,
    SocialAgentResponse,
    StoryAgentRequest,
    StoryAgentResponse,
    SupportAgentRequest,
    SupportAgentResponse,
)
from ..security import current_user_id, require_api_key

router = APIRouter(prefix="/agents", tags=["agents"], dependencies=[Depends(require_api_key)])


@router.post("/story", response_model=StoryAgentResponse)
async def story_agent(payload: StoryAgentRequest) -> StoryAgentResponse:
    """Runs the story agent. Nothing is saved: publish the draft with POST /stories.

    The agent reads the catalog, chooses a category, age range and premise
    (unless they're given), writes the story and revises it until it passes the
    catalog's checks. Send `draft` with a change request in `topic`, plus the
    `history` from the last response, to revise a story instead (the /studio chat).
    """
    try:
        draft, notes, steps, history = await run_story_agent(
            topic=payload.topic,
            category=payload.category,
            age_range=payload.age_range,
            draft=payload.draft,
            history=payload.history,
            source=payload.source,
        )
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return StoryAgentResponse(draft=draft, notes=notes, steps=steps, history=history)


@router.post("/social", response_model=SocialAgentResponse)
async def social_agent(payload: SocialAgentRequest) -> SocialAgentResponse:
    """Runs the social agent for a published story (or, with `kind: "books"`, a Book Picks
    digest): writes and publishes a post per platform.

    With `dryRun` the posts are written but not published. A platform that
    rejects its post shows up as `failed` in the result instead of failing the call.
    """
    try:
        posts, notes, steps = await run_social_agent(
            slug=payload.slug, kind=payload.kind, site_url=payload.site_url, dry_run=payload.dry_run
        )
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return SocialAgentResponse(posts=posts, notes=notes, steps=steps)


@router.post("/books", response_model=BooksAgentResponse)
async def books_agent(payload: BooksAgentRequest) -> BooksAgentResponse:
    """Runs the books agent: researches what children's books are selling and publishes a digest.

    The agent searches the web for current bestseller lists, picks an angle the
    recent digests didn't take (unless `focus` gives one), and publishes the
    digest to GET /books. Share it with POST /agents/social and `kind: "books"`.
    With `dryRun` it's written but not saved.
    """
    try:
        digest, notes, steps = await run_books_agent(focus=payload.focus, dry_run=payload.dry_run)
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return BooksAgentResponse(digest=digest, published=not payload.dry_run, notes=notes, steps=steps)


@router.post("/support", response_model=SupportAgentResponse)
async def support_agent(
    payload: SupportAgentRequest, user_id: str | None = Depends(current_user_id)
) -> SupportAgentResponse:
    """Runs the support agent on one message from the site's chat bubble.

    Forward the reader's Supabase token as `Authorization: Bearer <jwt>` so the
    agent knows whether they're signed in. The agent only
    reads the catalog; when it can't help, `handoff` says what to pass to the owner.
    """
    try:
        reply, handoff, steps, history = await run_support_agent(
            message=payload.message,
            history=payload.history,
            signed_in=user_id is not None,
        )
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return SupportAgentResponse(reply=reply, handoff=handoff, steps=steps, history=history)


@router.get("/activity", response_model=list[AgentRun])
async def agent_activity(seconds: int = Query(default=600, ge=1, le=86400)) -> list[AgentRun]:
    """Agent runs and story publishes started in the last `seconds`, newest first.

    The admin dashboard polls this to animate only what's actually running. A run
    still `running` long after it started was cut off (a serverless timeout).
    """
    try:
        rows = activity.recent(seconds)
    except APIError as exc:
        raise HTTPException(status_code=503, detail=f"Agent activity isn't set up on this server: {exc.message or exc}") from exc
    return [AgentRun.model_validate(row) for row in rows]
