"""The visitors agent: reads the site's visit numbers and tells the owner what they mean.

The visit tracker on the Nuxt site records every visit (an anonymous browser id,
how long the reader was really there, and the pages they read). This agent only
reads those numbers, already summed up, and turns them into a short report for
the owner: how many people came, how long they stayed, which stories hold
attention, what changed since the period before, and what might be worth trying.
Asked a question on the admin's Visitors page, it answers that instead.
"""

import json
from dataclasses import dataclass, field

from anthropic import beta_async_tool
from anthropic.lib.tools import ToolError
from postgrest.exceptions import APIError

from .. import activity, visitors
from ..schemas import AgentStep
from ..supabase_client import public_client
from .base import AgentError, run_agent

# The stats, a comparison, a title lookup, then the report.
MAX_ITERATIONS = 5
MAX_DAYS = 90

SYSTEM_PROMPT = f"""\
You are Kidstory's visitors agent. Kidstory is a website of short illustrated \
stories that parents read with children aged 3 to 12. You report to its owner on \
who visits the site and how long they stay, from the numbers its visit tracker \
records. A visitor is one browser; a visit ends after 30 minutes without \
activity; time on the site only counts while the tab is visible and the reader \
active. The owner's own browser isn't counted.

Use visitor_stats for the period asked about (and all-time totals), \
compare_periods to see what changed, and story_titles to name the stories behind \
/stories/... paths. Base every number you give on what the tools returned: never \
estimate or invent one. With few visits (under about 30), say the numbers are too \
small to read much into.

Write for the owner, plainly, in under 200 words: lead with visitors and time on \
the site, then what stands out (stories that hold readers, where visitors come \
from, a change since the last period), then one or two practical suggestions \
for the site. Give durations as minutes and seconds. Use short paragraphs or a \
few "- " bullets, no headings or tables. If the owner asked a question, answer \
it first and keep to it. The period is at most {MAX_DAYS} days."""


@dataclass
class VisitorsAgentRun:
    steps: list[AgentStep] = field(default_factory=list)

    def tools(self) -> list:
        @beta_async_tool
        async def visitor_stats(days: int = 7) -> str:
            """Visitors, visits, time on the site, top pages, referrers, devices and countries for the last
            `days` days (today included, Pakistan time), with a day-by-day series, all-time totals and how
            many visitors are on the site right now. Times are in seconds.

            Args:
                days: How many days back, 1 to 90.
            """
            if not 1 <= days <= MAX_DAYS:
                raise ToolError(f"days must be between 1 and {MAX_DAYS}.")
            stats = visitors.visitor_stats(days)
            period = stats["period"]
            self.steps.append(
                AgentStep(
                    tool="visitor_stats",
                    detail=f"{days} days: {period['visitors']} visitors, {period['visits']} visits, avg {period['avg_visit_seconds']}s",
                )
            )
            return json.dumps(stats)

        @beta_async_tool
        async def compare_periods(days: int = 7) -> str:
            """The last `days` days against the `days` before them: visitors, visits, time on the site,
            bounce rate and top pages for each.

            Args:
                days: The length of each period, 1 to 45.
            """
            if not 1 <= days <= MAX_DAYS // 2:
                raise ToolError(f"days must be between 1 and {MAX_DAYS // 2}.")
            result = visitors.compare_periods(days)
            self.steps.append(
                AgentStep(
                    tool="compare_periods",
                    detail=f"{days} days: {result['current']['visitors']} visitors vs {result['previous']['visitors']} before",
                )
            )
            return json.dumps(result)

        @beta_async_tool
        async def story_titles(paths: list[str]) -> str:
            """The titles of the stories at some /stories/... paths, to name them in the report.

            Args:
                paths: Up to 10 paths such as "/stories/the-brave-little-owl".
            """
            slugs = [p.strip().rstrip("/").removeprefix("/stories/") for p in paths[:10] if p.strip().startswith("/stories/")]
            titles: dict[str, str | None] = {}
            for slug in slugs:
                rows = public_client().table("stories").select("slug, title").eq("slug", slug).limit(1).execute().data or []
                titles[f"/stories/{slug}"] = rows[0]["title"] if rows else None
            self.steps.append(AgentStep(tool="story_titles", detail=f"{sum(1 for t in titles.values() if t)} of {len(slugs)} found"))
            return json.dumps(titles)

        return [visitor_stats, compare_periods, story_titles]


def _prompt(days: int, question: str | None) -> str:
    lines = [f"Report on the last {days} days."]
    if question and question.strip():
        # The owner's own words, fenced so they read as a question about the numbers.
        lines += ["The owner asks (between the markers):", "<<<question", question.strip(), "question>>>"]
    return "\n".join(lines)


async def run_visitors_agent(*, days: int = 7, question: str | None = None) -> tuple[str, list[AgentStep]]:
    """Runs the agent and records the run; returns the report and the steps it took."""
    with activity.tracked("visitors", days=days, question=bool(question and question.strip()) or None):
        try:
            # Fail before spending a Claude call when the visits table isn't there.
            visitors.all_time()
        except APIError as exc:
            raise AgentError(503, f"Visitor tracking isn't set up on this server: {exc.message or exc}") from exc
        run = VisitorsAgentRun()
        report = await run_agent(
            system=SYSTEM_PROMPT,
            prompt=_prompt(days, question),
            tools=run.tools(),
            max_iterations=MAX_ITERATIONS,
        )
        return report or "I couldn't write a report this time. Please try again.", run.steps
