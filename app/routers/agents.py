from fastapi import APIRouter, Depends, HTTPException

from ..agents.base import AgentError
from ..agents.social_agent import run_social_agent
from ..agents.story_agent import run_story_agent
from ..agents.support_agent import run_support_agent
from ..schemas import (
    SocialAgentRequest,
    SocialAgentResponse,
    StoryAgentRequest,
    StoryAgentResponse,
    SupportAgentRequest,
    SupportAgentResponse,
)
from ..security import current_user_id, has_full_access, require_api_key

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
        )
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return StoryAgentResponse(draft=draft, notes=notes, steps=steps, history=history)


@router.post("/social", response_model=SocialAgentResponse)
async def social_agent(payload: SocialAgentRequest) -> SocialAgentResponse:
    """Runs the social agent for a published story: writes and publishes a post per platform.

    With `dryRun` the posts are written but not published. A platform that
    rejects its post shows up as `failed` in the result instead of failing the call.
    """
    try:
        posts, notes, steps = await run_social_agent(
            slug=payload.slug, site_url=payload.site_url, dry_run=payload.dry_run
        )
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return SocialAgentResponse(posts=posts, notes=notes, steps=steps)


@router.post("/support", response_model=SupportAgentResponse)
async def support_agent(
    payload: SupportAgentRequest, user_id: str | None = Depends(current_user_id)
) -> SupportAgentResponse:
    """Runs the support agent on one message from the site's chat bubble.

    Forward the reader's Supabase token as `Authorization: Bearer <jwt>` so the
    agent knows whether they're signed in and have Full Access. The agent only
    reads the catalog; when it can't help, `handoff` says what to pass to the owner.
    """
    try:
        reply, handoff, steps, history = await run_support_agent(
            message=payload.message,
            history=payload.history,
            signed_in=user_id is not None,
            has_access=has_full_access(user_id),
        )
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return SupportAgentResponse(reply=reply, handoff=handoff, steps=steps, history=history)
