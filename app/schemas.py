from datetime import datetime
from typing import Annotated, Literal

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

    The body is deliberately omitted here for the same reason the Nuxt listing
    route omits it: fetching it would leak paywalled text to anyone listing stories.
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
    is_premium: bool


class Story(StorySummary):
    body: list[str]
    locked: bool = False
    locked_body_count: int | None = None


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
    is_premium: bool = True
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
    is_premium: bool | None = None
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
