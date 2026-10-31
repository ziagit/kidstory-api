from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

# Kept in sync with shared/utils/story-authoring.ts in the Nuxt app.
CategorySlug = Literal["adventure", "bedtime", "animals", "friendship", "fairy-tale", "funny"]
AgeRange = Literal["3-5", "6-8", "9-12", "all-ages"]

CATEGORY_SLUGS: tuple[str, ...] = ("adventure", "bedtime", "animals", "friendship", "fairy-tale", "funny")
AGE_RANGES: tuple[str, ...] = ("3-5", "6-8", "9-12", "all-ages")

CATEGORY_LABELS: dict[str, str] = {
    "adventure": "Adventure",
    "bedtime": "Bedtime",
    "animals": "Animals",
    "friendship": "Friendship",
    "fairy-tale": "Fairy Tales",
    "funny": "Funny Stories",
}


class CamelModel(BaseModel):
    """Serializes to camelCase, matching the Nuxt app's `mapStoryRow()` output."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


def _clean_paragraphs(value: list[str]) -> list[str]:
    return [p.strip() for p in value if isinstance(p, str) and p.strip()]


class StorySummary(CamelModel):
    """A story without its body — what the listing endpoint returns.

    The body is left out to keep listing pages small; fetch one story for its text.
    """

    slug: str
    title: str
    emoji: str
    excerpt: str
    category: str
    age_range: str
    read_time_minutes: int
    tags: list[str]
    cover_image_url: str | None
    published_at: datetime


class Story(StorySummary):
    body: list[str]


class StoryList(CamelModel):
    total: int
    limit: int
    offset: int
    items: list[StorySummary]


class StoryCreate(CamelModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    excerpt: Annotated[str, Field(min_length=1, max_length=1000)]
    body: Annotated[list[str], Field(min_length=1)]
    category: CategorySlug = "adventure"
    age_range: AgeRange = "all-ages"
    emoji: Annotated[str, Field(max_length=16)] = "✨"
    read_time_minutes: Annotated[int, Field(ge=1, le=5)] = 2
    tags: Annotated[list[str], Field(max_length=4)] = []
    # Optional overrides — omit and the API derives them (slug from the title,
    # cover from the Pollinations formula, published_at from the clock).
    slug: str | None = None
    cover_image_url: str | None = None
    published_at: datetime | None = None

    @field_validator("body")
    @classmethod
    def body_must_have_content(cls, value: list[str]) -> list[str]:
        cleaned = _clean_paragraphs(value)
        if not cleaned:
            raise ValueError("body must contain at least one non-empty paragraph")
        return cleaned

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        return [t.strip() for t in value if isinstance(t, str) and t.strip()]


class StoryUpdate(CamelModel):
    """Every field optional — only the ones supplied are written."""

    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    excerpt: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    body: Annotated[list[str], Field(min_length=1)] | None = None
    category: CategorySlug | None = None
    age_range: AgeRange | None = None
    emoji: Annotated[str, Field(max_length=16)] | None = None
    read_time_minutes: Annotated[int, Field(ge=1, le=5)] | None = None
    tags: Annotated[list[str], Field(max_length=4)] | None = None
    cover_image_url: str | None = None
    published_at: datetime | None = None

    @field_validator("body")
    @classmethod
    def body_must_have_content(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = _clean_paragraphs(value)
        if not cleaned:
            raise ValueError("body must contain at least one non-empty paragraph")
        return cleaned

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [t.strip() for t in value if isinstance(t, str) and t.strip()]


class Category(CamelModel):
    slug: str
    label: str


class Health(CamelModel):
    status: str
    database: str


class ChatMessage(CamelModel):
    role: Literal["user", "assistant"]
    content: str


class StoryDraft(CamelModel):
    """What the story agent returns — the same shape /studio's editor works with.

    Everything here is accepted as-is by POST /stories (StoryCreate), so a client
    can publish a draft without reshaping it.
    """

    title: str
    emoji: str
    excerpt: str
    category: CategorySlug
    age_range: AgeRange
    read_time_minutes: int
    tags: list[str]
    body: list[str]


class StoryAgentRequest(CamelModel):
    # A premise to build on (a /studio topic, or the Nuxt app's topic pool). Left
    # out, the agent picks its own premise from what the catalog is missing.
    # With `draft`, this is the change asked for instead ("make it shorter").
    topic: Annotated[str, Field(max_length=1000)] | None = None
    # Fixed when given; otherwise the agent chooses from the catalog's gaps (or,
    # for a revision, keeps the draft's unless the request changes them).
    category: CategorySlug | None = None
    age_range: AgeRange | None = None
    # The story to revise. Left out, the agent writes a new one.
    draft: StoryDraft | None = None
    # Earlier turns of a /studio conversation, as returned by the last call.
    # Trimmed server-side.
    history: list[ChatMessage] = []
    # What asked for the story, for the admin dashboard's activity view.
    source: Literal["studio", "cron", "partner", "mcp"] | None = None


class AgentStep(CamelModel):
    """One tool call the agent made, for logs and for seeing how it decided."""

    tool: str
    detail: str


class StoryAgentResponse(CamelModel):
    # Accepted by POST /stories as-is.
    draft: StoryDraft
    # The agent's own one-line account of why it wrote this story, or what it changed.
    notes: str
    steps: list[AgentStep]
    # The conversation including this turn, for the client to send back next time.
    history: list[ChatMessage]


class PartnerStoryRequest(CamelModel):
    # What the partner app wants a story about. The story agent writes every word.
    topic: Annotated[str, Field(min_length=3, max_length=300)]
    # Fixed when given; otherwise the agent picks what the catalog is short on.
    category: CategorySlug | None = None
    age_range: AgeRange | None = None


class PartnerStoryResponse(CamelModel):
    # The published story, as GET /stories/{slug} returns it.
    story: Story
    # Where it lives on the site.
    url: str
    # The agent's one-line account of why it wrote it this way.
    notes: str


SocialPlatform = Literal["facebook", "instagram", "email", "youtube"]


class SocialAgentRequest(CamelModel):
    slug: Annotated[str, Field(min_length=1, max_length=200)]
    # What `slug` names: a story, or a Book Picks digest (POST /agents/books).
    kind: Literal["story", "books"] = "story"
    # Origin the story links point at. Defaults to SITE_URL.
    site_url: str | None = None
    # Write the posts but don't publish them — for checking the agent's output.
    dry_run: bool = False


class SocialPost(CamelModel):
    platform: SocialPlatform
    # posted: live on the platform. dry_run: written, not published.
    # skipped: not configured, or the agent chose not to post. failed: the platform rejected it.
    status: Literal["posted", "dry_run", "skipped", "failed"]
    text: str | None = None
    post_id: str | None = None
    error: str | None = None


class SocialAgentResponse(CamelModel):
    posts: list[SocialPost]
    notes: str
    steps: list[AgentStep]


class SupportAgentRequest(CamelModel):
    # What the reader just typed into the chat bubble.
    message: Annotated[str, Field(min_length=1, max_length=1000)]
    # Earlier turns of the chat, as returned by the last call. Trimmed server-side.
    history: Annotated[list[ChatMessage], Field(max_length=40)] = []


class SupportHandoff(CamelModel):
    """A conversation the agent passed to the owner: the Nuxt app emails it on."""

    summary: str
    # What the reader gave; None when they're signed in (their account email applies).
    contact_email: str | None = None


class SupportAgentResponse(CamelModel):
    reply: str
    # Set when the agent handed the conversation to the owner this turn.
    handoff: SupportHandoff | None = None
    steps: list[AgentStep]
    # The conversation including this turn, for the client to send back next time.
    history: list[ChatMessage]


class BookPick(CamelModel):
    """One book a digest names."""

    title: str
    author: str
    # Free text from the agent, e.g. "3-5" or "8-12".
    ages: str
    # One sentence on who it suits and why children like it.
    why: str


class DigestSource(CamelModel):
    title: str
    url: str


class BookDigestSummary(CamelModel):
    """A Book Picks digest without its text, for listings."""

    slug: str
    title: str
    emoji: str
    excerpt: str
    cover_image_url: str | None = None
    published_at: datetime


class BookDigest(BookDigestSummary):
    """A Book Picks digest: the books agent's research on what children's books are selling."""

    body: list[str]
    books: list[BookPick]
    sources: list[DigestSource]


class BookDigestList(CamelModel):
    total: int
    limit: int
    offset: int
    items: list[BookDigestSummary]


class BooksAgentRequest(CamelModel):
    # An angle to take ("picture books", "middle grade fantasy"). Left out, the
    # agent picks one the recent digests didn't cover.
    focus: Annotated[str, Field(max_length=300)] | None = None
    # Research and write the digest, but don't save it.
    dry_run: bool = False


class BooksAgentResponse(CamelModel):
    digest: BookDigest
    # False for a dry run.
    published: bool
    # The agent's one-line account of the angle it chose.
    notes: str
    steps: list[AgentStep]


class AgentRun(CamelModel):
    """One agent run (or story publish), for the admin dashboard's live agent machine."""

    id: str
    agent: Literal["story", "social", "books", "support", "visitors"]
    # "run", or "publish" for a story going live.
    action: str
    status: Literal["running", "done", "failed"]
    # What started it and where its work went, e.g. {"source": "studio"} or {"platforms": ["facebook"]}.
    detail: dict[str, Any] = {}
    started_at: datetime
    finished_at: datetime | None = None


class VisitorsAgentRequest(CamelModel):
    # How many days back the report covers, today included.
    days: Annotated[int, Field(ge=1, le=90)] = 7
    # A question from the owner about the visit numbers. Left out, the agent writes its usual report.
    question: Annotated[str, Field(max_length=500)] | None = None


class VisitorsAgentResponse(CamelModel):
    # The agent's report (or answer), in short paragraphs or "- " bullets.
    report: str
    steps: list[AgentStep]
