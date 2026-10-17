from fastapi import APIRouter, Depends, HTTPException, status

from ..agents.base import AgentError
from ..agents.story_agent import run_story_agent
from ..config import get_settings
from ..partners import PartnerApp, claim_run, require_partner
from ..schemas import PartnerStoryRequest, PartnerStoryResponse, StoryCreate
from .stories import insert_story

router = APIRouter(prefix="/partner", tags=["partner"])


@router.post("/stories", response_model=PartnerStoryResponse, status_code=status.HTTP_201_CREATED)
async def create_partner_story(
    payload: PartnerStoryRequest, partner: PartnerApp = Depends(require_partner)
) -> PartnerStoryResponse:
    """For partner apps: the story agent writes a story about `topic` and it's published.

    Needs `Authorization: Bearer <partner key>`. The agent treats the topic as a
    suggestion, writes every field itself and checks the story against the
    catalog's rules; it may decline a topic that isn't right for children (422).
    Each run counts against the app's daily limit (429 once it's used up).
    """
    claim_run(partner)
    try:
        draft, notes, _, _ = await run_story_agent(
            topic=payload.topic,
            category=payload.category,
            age_range=payload.age_range,
            requested_by=partner.name,
        )
    except AgentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    story = insert_story(StoryCreate(**draft.model_dump()), created_by=partner.name)
    url = f"{get_settings().site_url.rstrip('/')}/stories/{story.slug}"
    return PartnerStoryResponse(story=story, url=url, notes=notes)
