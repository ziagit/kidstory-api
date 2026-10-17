from fastapi import APIRouter, Depends, HTTPException

from ..agents.base import AgentError
from ..agents.social_agent import run_social_agent
from ..agents.story_agent import run_story_agent
from ..schemas import SocialAgentRequest, SocialAgentResponse, StoryAgentRequest, StoryAgentResponse
from ..security import require_api_key

router = APIRouter(prefix="/agents", tags=["agents"], dependencies=[Depends(require_api_key)])


@router.post("/story", response_model=StoryAgentResponse)
async def story_agent(payload: StoryAgentRequest) -> StoryAgentResponse:
    """Runs the story agent. Nothing is saved: publish the draft with POST /stories.

    The agent reads the catalog, chooses a category, age range and premise
    (unless they're given), writes the story and revises it until it passes the
    catalog's checks.
    """
    try:
        draft, notes, steps = await run_story_agent(
            topic=payload.topic, category=payload.category, age_range=payload.age_range
        )
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return StoryAgentResponse(draft=draft, notes=notes, steps=steps)


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
