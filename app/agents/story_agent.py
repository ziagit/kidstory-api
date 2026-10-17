"""The story agent: every new story and every revision of one is written here.

For a new story it looks at the catalog first, so it can fill gaps in categories
and age bands and steer clear of premises that already exist. Given a draft and
a change request (the /studio chat's follow-ups), it revises that draft instead.
Either way it writes every field itself, and `submit_story` holds it to the
catalog's rules, sending problems back for the agent to fix rather than failing
the run.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field

from anthropic import beta_async_tool
from anthropic.lib.tools import ToolError

from ..routers.stories import slugify
from ..schemas import AGE_RANGES, CATEGORY_SLUGS, AgentStep, ChatMessage, StoryDraft
from ..supabase_client import public_client
from .base import AgentError, run_agent

# One look at the catalog, a search or two, a draft and a revision or two.
MAX_ITERATIONS = 8

MIN_WORDS, MAX_WORDS = 150, 450
MIN_PARAGRAPHS, MAX_PARAGRAPHS = 2, 4
RECENT_TITLES = 15
SEARCH_LIMIT = 10
# Earlier chat turns sent with a revision. Matches the 12 messages the Nuxt app
# keeps; the draft itself travels separately, so these are short.
MAX_HISTORY_TURNS = 12
# Words per minute a young reader (or a parent reading aloud) gets through.
WORDS_PER_MINUTE = 120

SYSTEM_PROMPT = f"""\
You write new stories for Kidstory, a website of short illustrated stories that \
parents read with children aged 3 to 12. Each run adds one story to the catalog.

Start with catalog_overview. When the category or age range isn't fixed for you, \
choose the ones the catalog has fewest stories in. Use search_catalog to make sure \
your premise isn't a near-copy of a story that already exists. Then write the story \
and submit it with submit_story.

A Kidstory story has {MIN_PARAGRAPHS} to {MAX_PARAGRAPHS} short paragraphs \
({MIN_WORDS} to {MAX_WORDS} words), a kind main character, a gentle problem solved \
through kindness, courage or cleverness, and a happy ending. Nothing scary, violent \
or mean-spirited. Match the vocabulary and sentence length to the age range.

When you're given a draft to revise, change what the request asks for and keep \
the rest. You don't need catalog_overview for a revision; use search_catalog only \
if the request changes the premise. Submit the whole revised story with submit_story.

If submit_story reports problems, fix them and submit again. Once a story is \
accepted, reply with one sentence: for a new story, why you chose it; for a \
revision, what you changed."""

WORD = re.compile(r"\b[\w'’-]+\b")


@dataclass
class StoryAgentRun:
    """Per-run state: what's fixed, what the agent did, and what it submitted."""

    category: str | None
    age_range: str | None
    steps: list[AgentStep] = field(default_factory=list)
    draft: StoryDraft | None = None

    def tools(self) -> list:
        @beta_async_tool
        async def catalog_overview() -> str:
            """How many stories the catalog has per category and age range, plus the newest titles."""
            rows = (
                public_client()
                .table("stories")
                .select("title, category, age_range, published_at")
                .order("published_at", desc=True)
                .execute()
                .data
                or []
            )
            by_category = Counter(row["category"] for row in rows)
            by_age = Counter(row["age_range"] for row in rows)
            self.steps.append(AgentStep(tool="catalog_overview", detail=f"{len(rows)} stories"))
            return json.dumps(
                {
                    "total": len(rows),
                    "byCategory": {slug: by_category.get(slug, 0) for slug in CATEGORY_SLUGS},
                    "byAgeRange": {age: by_age.get(age, 0) for age in AGE_RANGES},
                    "newestTitles": [row["title"] for row in rows[:RECENT_TITLES]],
                }
            )

        @beta_async_tool
        async def search_catalog(query: str) -> str:
            """Finds existing stories whose title or excerpt contains the query.

            Args:
                query: A word or short phrase, e.g. "dragon" or "lost kite".
            """
            term = re.sub(r'[\\"]', "", query.strip())
            if not term:
                raise ToolError("Give a word or phrase to search for.")
            rows = (
                public_client()
                .table("stories")
                .select("title, excerpt, category")
                .or_(f'title.ilike."*{term}*",excerpt.ilike."*{term}*"')
                .limit(SEARCH_LIMIT)
                .execute()
                .data
                or []
            )
            self.steps.append(AgentStep(tool="search_catalog", detail=f"{term!r}: {len(rows)} found"))
            if not rows:
                return f"No stories match {term!r}."
            return json.dumps(rows)

        @beta_async_tool
        async def submit_story(
            title: str,
            emoji: str,
            excerpt: str,
            category: str,
            age_range: str,
            tags: list[str],
            paragraphs: list[str],
        ) -> str:
            """Submits the finished story. Returns the problems to fix if it doesn't meet the catalog's rules.

            Args:
                title: The story's title, under 80 characters.
                emoji: One emoji for the story card.
                excerpt: One or two sentences that make a parent want to read on, under 200 characters.
                category: One of adventure, bedtime, animals, friendship, fairy-tale, funny.
                age_range: One of 3-5, 6-8, 9-12, all-ages.
                tags: One to four short lowercase tags, e.g. "kindness" or "owl".
                paragraphs: The story text, one string per paragraph.
            """
            if self.draft is not None:
                raise ToolError("A story was already accepted this run. Reply with your one-sentence note.")
            body = [p.strip() for p in paragraphs if p.strip()]
            clean_tags = [t.strip().lower() for t in tags if t.strip()]
            problems = self._problems(title.strip(), emoji.strip(), excerpt.strip(), category, age_range, clean_tags, body)
            self.steps.append(
                AgentStep(tool="submit_story", detail=f"{title.strip()!r}: " + ("; ".join(problems) or "accepted"))
            )
            if problems:
                raise ToolError("Not accepted yet. Fix these and submit again:\n- " + "\n- ".join(problems))
            self.draft = StoryDraft(
                title=title.strip(),
                emoji=emoji.strip(),
                excerpt=excerpt.strip(),
                category=category,
                age_range=age_range,
                read_time_minutes=_read_time_minutes(body),
                tags=clean_tags,
                body=body,
            )
            return "Accepted."

        return [catalog_overview, search_catalog, submit_story]

    def _problems(
        self,
        title: str,
        emoji: str,
        excerpt: str,
        category: str,
        age_range: str,
        tags: list[str],
        body: list[str],
    ) -> list[str]:
        problems = []
        if not title or len(title) > 80:
            problems.append("The title must be 1 to 80 characters.")
        elif _title_taken(title):
            problems.append(f"A story titled {title!r} already exists; pick a different title.")
        if not emoji or len(emoji) > 16:
            problems.append("Give exactly one emoji.")
        if not excerpt or len(excerpt) > 200:
            problems.append("The excerpt must be 1 to 200 characters.")
        if category not in CATEGORY_SLUGS:
            problems.append(f"Category must be one of: {', '.join(CATEGORY_SLUGS)}.")
        elif self.category and category != self.category:
            problems.append(f"The category is fixed to {self.category!r} for this run.")
        if age_range not in AGE_RANGES:
            problems.append(f"Age range must be one of: {', '.join(AGE_RANGES)}.")
        elif self.age_range and age_range != self.age_range:
            problems.append(f"The age range is fixed to {self.age_range!r} for this run.")
        if not 1 <= len(tags) <= 4:
            problems.append("Give one to four tags.")
        if not MIN_PARAGRAPHS <= len(body) <= MAX_PARAGRAPHS:
            problems.append(f"Use {MIN_PARAGRAPHS} to {MAX_PARAGRAPHS} paragraphs (you sent {len(body)}).")
        words = sum(len(WORD.findall(p)) for p in body)
        if not MIN_WORDS <= words <= MAX_WORDS:
            problems.append(f"The story must be {MIN_WORDS} to {MAX_WORDS} words (it is {words}).")
        return problems


def _read_time_minutes(body: list[str]) -> int:
    words = sum(len(p.split()) for p in body)
    return max(1, min(3, round(words / WORDS_PER_MINUTE) or 1))


def _title_taken(title: str) -> bool:
    # By slug, since that's what would collide, and by title, since older
    # stories (seeded or retitled) don't always have a slug derived from it.
    table = public_client().table
    return any(
        table("stories").select("slug").eq(column, value).limit(1).execute().data
        for column, value in (("slug", slugify(title)), ("title", title))
    )


def _prompt(topic: str | None, category: str | None, age_range: str | None, draft: StoryDraft | None) -> str:
    if draft is not None:
        lines = [
            "Revise this draft.",
            f"Change requested: {topic.strip()}" if topic and topic.strip() else "Change requested: improve it.",
        ]
    else:
        lines = ["Write today's new story."]
        lines.append(f"Premise to build on: {topic.strip()}" if topic and topic.strip() else "Choose the premise yourself.")
    if category:
        lines.append(f"Category (fixed): {category}")
    if age_range:
        lines.append(f"Age range (fixed): {age_range}")
    if draft is not None:
        current = {
            "title": draft.title,
            "emoji": draft.emoji,
            "excerpt": draft.excerpt,
            "category": draft.category,
            "age_range": draft.age_range,
            "tags": draft.tags,
            "paragraphs": draft.body,
        }
        lines += ["", "Current draft:", json.dumps(current, ensure_ascii=False, indent=2)]
    return "\n".join(lines)


async def run_story_agent(
    *,
    topic: str | None = None,
    category: str | None = None,
    age_range: str | None = None,
    draft: StoryDraft | None = None,
    history: list[ChatMessage] | None = None,
) -> tuple[StoryDraft, str, list[AgentStep], list[ChatMessage]]:
    """Runs the agent; returns the accepted draft, its note, the steps it took and the updated history.

    With `draft`, the agent revises it as `topic` asks. `history` is the
    conversation so far (the /studio chat); the returned history includes this turn.
    """
    prior = [m for m in (history or []) if m.content.strip()][-MAX_HISTORY_TURNS:]
    run = StoryAgentRun(category=category, age_range=age_range)
    notes = await run_agent(
        system=SYSTEM_PROMPT,
        prompt=_prompt(topic, category, age_range, draft),
        tools=run.tools(),
        max_iterations=MAX_ITERATIONS,
        history=prior,
    )
    if run.draft is None:
        raise AgentError(502, "The story agent finished without an accepted story. Please try again.")
    request = topic.strip() if topic and topic.strip() else "Write a new story."
    turn = [ChatMessage(role="user", content=request), ChatMessage(role="assistant", content=notes or "Done.")]
    return run.draft, notes, run.steps, [*prior, *turn]
