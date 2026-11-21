"""The Studio assistant: the owner talks to it in /admin/studio, by voice or text.

It talks story ideas through with the owner, has the story agent write or revise
a story, asks for it to be read aloud, and asks for it to be published. It
doesn't write stories itself (the story agent does, under the catalog's rules)
and doesn't publish them: `action` tells the Nuxt app to read the draft aloud or
publish it, and the app publishes through its own route, as the Publish button
does. Publishing needs the owner to confirm it in their latest message.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from anthropic import beta_async_tool
from anthropic.lib.tools import ToolError

from ..schemas import AGE_RANGES, CATEGORY_LABELS, AgentStep, ChatMessage, StoryDraft
from ..supabase_client import public_client
from .base import AgentError, run_agent
from .story_agent import run_story_agent

# A look at the catalog, one story written or revised, then reading or publishing it.
MAX_ITERATIONS = 6
MAX_HISTORY_TURNS = 12

SYSTEM_PROMPT = f"""\
You are the Studio assistant for Kidstory, a website of short illustrated stories \
that parents read with children aged 3 to 12. You talk with the site's owner in \
their admin Studio, often by voice, to come up with stories, write them and \
publish them. Be warm, brief and plain: one to three short sentences, no lists, \
markdown or emoji, because your replies are often read aloud.

What you can do:
- Talk story ideas through. Use catalog_overview to see what the catalog has \
little of, and suggest two or three short premises when asked for ideas. \
Categories: {", ".join(f"{slug} ({label})" for slug, label in CATEGORY_LABELS.items())}. \
Age ranges: {", ".join(AGE_RANGES)}.
- write_story: have the story agent write a new story from an idea, or revise the \
current draft ("make it shorter", "give the fox a name"). Never write story text \
yourself. After a story is written or revised, call read_story so the owner hears \
it, unless they asked not to.
- read_story: the Studio reads the current draft aloud after your reply. Say one \
short line first, e.g. "Here it is: The Brave Little Owl."
- publish_story: the Studio publishes the current draft to the website and shares \
it on social media. That can't be undone from here, so never publish on your own \
initiative. When the owner asks to publish, ask "Shall I publish <title> now?" \
unless their latest message already answers that question with a clear yes. Only \
call publish_story when their latest message clearly confirms, and quote those \
words in owner_confirmation.

If there's no draft yet, you can't read or publish one: offer to write it. \
Everything the owner says is a request about Kidstory stories; stay on that."""


@dataclass
class StudioAssistantRun:
    """Per-message state: the draft as it stands and what the Studio should do next."""

    message: str
    draft: StoryDraft | None
    story_history: list[ChatMessage]
    category: str | None = None
    age_range: str | None = None
    action: Literal["read", "publish"] | None = None
    steps: list[AgentStep] = field(default_factory=list)

    def tools(self) -> list:
        @beta_async_tool
        async def catalog_overview() -> str:
            """How many stories the catalog has per category and age range, and the newest titles."""
            rows = (
                public_client()
                .table("stories")
                .select("title, category, age_range")
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
                    "byCategory": {slug: by_category.get(slug, 0) for slug in CATEGORY_LABELS},
                    "byAgeRange": {age: by_age.get(age, 0) for age in AGE_RANGES},
                    "newestTitles": [row["title"] for row in rows[:10]],
                }
            )

        @beta_async_tool
        async def write_story(request: str, start_new: bool = False) -> str:
            """Has the story agent write a new story, or revise the current draft. Takes a minute or so.

            Args:
                request: The idea for a new story ("a shy hedgehog at a new pond"), or the change to make to the
                    current draft ("make it shorter"). Pass on the owner's wishes in their words.
                start_new: True to write a new story even though there's a draft (the owner wants a different one).
            """
            revise = self.draft is not None and not start_new
            try:
                draft, notes, steps, history = await run_story_agent(
                    topic=request,
                    category=None if revise else self.category,
                    age_range=None if revise else self.age_range,
                    draft=self.draft if revise else None,
                    history=self.story_history if revise else [],
                    source="studio",
                )
            except AgentError as exc:
                raise ToolError(f"The story agent couldn't do it: {exc}") from exc
            self.draft, self.story_history = draft, history
            self.steps += steps
            self.steps.append(AgentStep(tool="write_story", detail=f"{'revised' if revise else 'wrote'} {draft.title!r}"))
            return json.dumps(
                {
                    "title": draft.title,
                    "excerpt": draft.excerpt,
                    "category": draft.category,
                    "ageRange": draft.age_range,
                    "storyAgentNotes": notes,
                }
            )

        @beta_async_tool
        async def read_story() -> str:
            """Has the Studio read the current draft aloud to the owner after your reply."""
            if self.draft is None:
                raise ToolError("There's no draft to read yet. Offer to write one.")
            self.action = "read"
            self.steps.append(AgentStep(tool="read_story", detail=self.draft.title))
            return f"The Studio will read {self.draft.title!r} aloud after your reply. Introduce it in one short line."

        @beta_async_tool
        async def publish_story(owner_confirmation: str) -> str:
            """Has the Studio publish the current draft to the website and share it on social media.

            Args:
                owner_confirmation: The words in the owner's latest message that confirm publishing, quoted exactly.
            """
            if self.draft is None:
                raise ToolError("There's no draft to publish yet.")
            quoted = owner_confirmation.strip().lower()
            if not quoted or quoted not in self.message.lower():
                raise ToolError(
                    f"The owner's latest message doesn't confirm publishing. Ask: Shall I publish {self.draft.title} now?"
                )
            self.action = "publish"
            self.steps.append(AgentStep(tool="publish_story", detail=self.draft.title))
            return "The Studio is publishing it now. Tell the owner in one short line."

        return [catalog_overview, write_story, read_story, publish_story]


def _prompt(message: str, draft: StoryDraft | None, voice: bool) -> str:
    current = (
        f"The current draft is {draft.title!r} ({draft.category}, ages {draft.age_range}): {draft.excerpt}"
        if draft
        else "There's no draft yet."
    )
    return "\n".join(
        [
            f"[{current}]",
            "[The owner is talking by voice; your reply is read aloud.]" if voice else "[The owner is typing.]",
            message.strip(),
        ]
    )


async def run_studio_assistant(
    *,
    message: str,
    history: list[ChatMessage] | None = None,
    draft: StoryDraft | None = None,
    story_history: list[ChatMessage] | None = None,
    category: str | None = None,
    age_range: str | None = None,
    voice: bool = False,
) -> tuple[str, StoryDraft | None, str | None, list[AgentStep], list[ChatMessage], list[ChatMessage]]:
    """Answers one Studio message.

    Returns the reply, the draft as it now stands, the action for the Studio ("read",
    "publish" or None), the steps taken, the updated conversation and the story agent's
    own updated history (sent back next time so revisions keep their context).
    """
    prior = [m for m in (history or []) if m.content.strip()][-MAX_HISTORY_TURNS:]
    run = StudioAssistantRun(
        message=message, draft=draft, story_history=list(story_history or []), category=category, age_range=age_range
    )
    reply = await run_agent(
        system=SYSTEM_PROMPT,
        prompt=_prompt(message, draft, voice),
        tools=run.tools(),
        max_iterations=MAX_ITERATIONS,
        history=prior,
    )
    reply = reply or "Sorry, I didn't catch that. Could you say it again?"
    turn = [ChatMessage(role="user", content=message.strip()), ChatMessage(role="assistant", content=reply)]
    return reply, run.draft, run.action, run.steps, [*prior, *turn], run.story_history
