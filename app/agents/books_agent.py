"""The books agent: researches what children's books are selling and writes a digest.

Once a day it searches the web for current bestseller lists, picks an angle the
last few digests didn't take (picture books, early readers, middle grade, a new
series), and writes a short digest in its own words for Kidstory's parents.
`publish_digest` holds it to the rules: every book named, every source cited,
and every source a page its own searches turned up, so it can't invent one.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from anthropic import beta_async_tool
from anthropic.lib.tools import ToolError
from postgrest.exceptions import APIError

from .. import activity
from ..cover_image import build_cover_image_url
from ..routers.stories import slugify
from ..schemas import AgentStep, BookDigest, BookPick, DigestSource
from ..supabase_client import admin_client, public_client
from .base import AgentError, run_agent

# A look at recent digests, the searches (they run inside Claude's calls), a
# submission and a fix or two.
MAX_ITERATIONS = 6
# Web searches per run. Each one is billed; this covers the main lists and a check or two.
MAX_SEARCHES = 8
# Per Claude call: a call that runs searches takes far longer than a plain turn.
TIMEOUT_SECONDS = 150.0
RECENT_DIGESTS = 7

MIN_WORDS, MAX_WORDS = 180, 500
MIN_PARAGRAPHS, MAX_PARAGRAPHS = 3, 5
MIN_BOOKS, MAX_BOOKS = 3, 8
MIN_SOURCES = 2

# The basic search tool, not web_search_20260209: that one runs its searches from
# code in a sandbox, and when the code crashed (2026-10-28) it re-ran the same
# search until max_uses ran out. The basic one searches directly, in parallel.
WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES}

SYSTEM_PROMPT = f"""\
You write the daily "Book Picks" digest for Kidstory, a website of short stories \
that parents read with children aged 3 to 12. Each digest tells parents which \
children's books are selling best right now and which might suit their child.

Start with recent_digests so today's digest takes a different angle: one age band \
(picture books, early readers, middle grade), a genre, a new release climbing the \
charts, or a series. Then use web_search to find current bestseller lists for \
children's books (for example the New York Times, Publishers Weekly, USA Today, \
Amazon or big booksellers) and check what's on them this week.

Write {MIN_PARAGRAPHS} to {MAX_PARAGRAPHS} short paragraphs ({MIN_WORDS} to \
{MAX_WORDS} words) in your own words: what's selling, who each book suits, and \
why children like it. Name {MIN_BOOKS} to {MAX_BOOKS} books with their authors. \
Describe a book only with what you're sure of; if you're not, say who it suits \
and leave the plot out. \
Say where the rankings come from. Only include books your searches show are \
selling well, and never invent rankings, sales numbers or quotes. Don't copy \
publisher blurbs or reviews. Keep it warm and plain, for parents, and leave out \
any book that isn't suitable for children.

Submit with publish_digest, citing at least {MIN_SOURCES} pages your searches \
returned. If it reports problems, fix them and submit again. Once it's accepted, \
reply with only one plain sentence on the angle you chose."""

WORD = re.compile(r"\b[\w'’-]+\b")


@dataclass
class BooksAgentRun:
    """Per-run state: what the searches returned, what the agent did, and what it published."""

    dry_run: bool
    seen_urls: set[str] = field(default_factory=set)
    steps: list[AgentStep] = field(default_factory=list)
    digest: BookDigest | None = None

    def collect(self, message: Any) -> None:
        """Records the URLs this response's web searches returned (and any it cited)."""
        for block in message.content:
            if block.type == "web_search_tool_result":
                results = block.content if isinstance(block.content, list) else []
                self.seen_urls.update(r.url for r in results if getattr(r, "url", None))
                query = next(
                    (
                        b.input.get("query", "")
                        for b in message.content
                        if b.type == "server_tool_use" and b.id == block.tool_use_id
                    ),
                    "",
                )
                detail = f"{query!r}: {len(results)} results" if results else f"{query!r}: failed"
                self.steps.append(AgentStep(tool="web_search", detail=detail))
            elif block.type == "text":
                for citation in getattr(block, "citations", None) or []:
                    if getattr(citation, "url", None):
                        self.seen_urls.add(citation.url)

    def tools(self) -> list:
        @beta_async_tool
        async def recent_digests() -> str:
            """The latest Book Picks digests: their titles, dates and the books they covered."""
            try:
                rows = _recent()
            except APIError:
                # Only reachable on a dry run: a real run checks the table first.
                rows = []
            self.steps.append(AgentStep(tool="recent_digests", detail=f"{len(rows)} digests"))
            if not rows:
                return "No digests yet: this is the first one."
            return json.dumps(
                [
                    {
                        "title": row["title"],
                        "date": str(row["published_at"])[:10],
                        "books": [b.get("title") for b in row.get("books") or []],
                    }
                    for row in rows
                ]
            )

        @beta_async_tool
        async def publish_digest(
            title: str,
            emoji: str,
            excerpt: str,
            paragraphs: list[str],
            books: list[dict],
            sources: list[dict],
        ) -> str:
            """Publishes the digest. Returns the problems to fix if it doesn't meet the rules.

            Args:
                title: The digest's headline, under 80 characters, e.g. "Picture Books Topping the Charts This Week".
                emoji: One emoji for the digest card.
                excerpt: One or two sentences for the card and for sharing, under 200 characters.
                paragraphs: The digest text, one string per paragraph.
                books: Each book named, as {"title", "author", "ages", "why"}: "ages" like "3-5" or "8-12", and "why" one sentence under 200 characters.
                sources: The pages the rankings come from, as {"title", "url"}; each url must be one your searches returned.
            """
            if self.digest is not None:
                raise ToolError("A digest was already accepted this run. Reply with your one-sentence note.")
            body = [p.strip() for p in paragraphs if isinstance(p, str) and p.strip()]
            picks, pick_problems = _picks(books)
            cited, source_problems = _sources(sources, self.seen_urls)
            problems = _problems(title.strip(), emoji.strip(), excerpt.strip(), body) + pick_problems + source_problems
            self.steps.append(
                AgentStep(tool="publish_digest", detail=f"{title.strip()!r}: " + ("; ".join(problems) or "accepted"))
            )
            if problems:
                raise ToolError("Not accepted yet. Fix these and submit again:\n- " + "\n- ".join(problems))
            self.digest = _save(title.strip(), emoji.strip(), excerpt.strip(), body, picks, cited, dry_run=self.dry_run)
            return "Accepted." if not self.dry_run else "Accepted (dry run: not saved)."

        return [WEB_SEARCH, recent_digests, publish_digest]


def _recent() -> list[dict]:
    return (
        public_client()
        .table("book_digests")
        .select("title, published_at, books")
        .order("published_at", desc=True)
        .limit(RECENT_DIGESTS)
        .execute()
        .data
        or []
    )


def _problems(title: str, emoji: str, excerpt: str, body: list[str]) -> list[str]:
    problems = []
    if not title or len(title) > 80:
        problems.append("The title must be 1 to 80 characters.")
    if not emoji or len(emoji) > 16:
        problems.append("Give exactly one emoji.")
    if not excerpt or len(excerpt) > 200:
        problems.append("The excerpt must be 1 to 200 characters.")
    if not MIN_PARAGRAPHS <= len(body) <= MAX_PARAGRAPHS:
        problems.append(f"Use {MIN_PARAGRAPHS} to {MAX_PARAGRAPHS} paragraphs (you sent {len(body)}).")
    words = sum(len(WORD.findall(p)) for p in body)
    if not MIN_WORDS <= words <= MAX_WORDS:
        problems.append(f"The digest must be {MIN_WORDS} to {MAX_WORDS} words (it is {words}).")
    return problems


def _picks(books: list[dict]) -> tuple[list[BookPick], list[str]]:
    picks, problems = [], []
    for i, book in enumerate(books if isinstance(books, list) else [], start=1):
        if not isinstance(book, dict):
            problems.append(f"Book {i} must be an object with title, author, ages and why.")
            continue
        fields = {key: str(book.get(key) or "").strip() for key in ("title", "author", "ages", "why")}
        missing = [key for key, value in fields.items() if not value]
        if missing:
            problems.append(f"Book {i} ({fields['title'] or 'untitled'}) is missing: {', '.join(missing)}.")
        elif len(fields["why"]) > 200:
            problems.append(f"Keep the 'why' for {fields['title']!r} under 200 characters.")
        else:
            picks.append(BookPick(**fields))
    if not MIN_BOOKS <= len(books if isinstance(books, list) else []) <= MAX_BOOKS:
        problems.append(f"Name {MIN_BOOKS} to {MAX_BOOKS} books.")
    return picks, problems


def _sources(sources: list[dict], seen_urls: set[str]) -> tuple[list[DigestSource], list[str]]:
    cited, problems = [], []
    for source in sources if isinstance(sources, list) else []:
        url = str((source or {}).get("url") or "").strip() if isinstance(source, dict) else ""
        title = str((source or {}).get("title") or "").strip() if isinstance(source, dict) else ""
        if not url.startswith(("https://", "http://")):
            problems.append(f"{url or 'A source'} isn't a web address.")
        elif url not in seen_urls:
            problems.append(f"{url} wasn't in your search results. Cite only pages your searches returned.")
        else:
            cited.append(DigestSource(title=title or url, url=url))
    if len(cited) < MIN_SOURCES and not problems:
        problems.append(f"Cite at least {MIN_SOURCES} sources from your searches.")
    return cited, problems


def _save(
    title: str,
    emoji: str,
    excerpt: str,
    body: list[str],
    picks: list[BookPick],
    sources: list[DigestSource],
    *,
    dry_run: bool,
) -> BookDigest:
    now = datetime.now(timezone.utc)
    slug = slugify(title)
    if not dry_run and _slug_taken(slug):
        # The same headline on another day: the date keeps the URL unique.
        slug = f"{slug}-{now:%Y-%m-%d}"
    digest = BookDigest(
        slug=slug,
        title=title,
        emoji=emoji,
        excerpt=excerpt,
        body=body,
        books=picks,
        sources=sources,
        cover_image_url=build_cover_image_url(title, "children's books and reading", slug),
        published_at=now,
    )
    if not dry_run:
        row = digest.model_dump(mode="json")
        admin_client().table("book_digests").insert(row).execute()
    return digest


def _slug_taken(slug: str) -> bool:
    return bool(public_client().table("book_digests").select("slug").eq("slug", slug).limit(1).execute().data)


def _prompt(focus: str | None) -> str:
    today = datetime.now(timezone.utc).strftime("%A %d %B %Y")
    lines = [f"Today is {today}. Write today's Book Picks digest."]
    if focus and focus.strip():
        lines.append(f"Angle to take: {focus.strip()}")
    else:
        lines.append("Choose the angle yourself.")
    return "\n".join(lines)


async def run_books_agent(
    *, focus: str | None = None, dry_run: bool = False
) -> tuple[BookDigest, str, list[AgentStep]]:
    """Runs the agent and records the run, with the digest it saved."""
    with activity.tracked("books", dry_run=dry_run or None) as log:
        digest, notes, steps = await _run_books_agent(focus=focus, dry_run=dry_run)
        if not dry_run:
            log.detail["published"] = digest.slug
        return digest, notes, steps


async def _run_books_agent(*, focus: str | None, dry_run: bool) -> tuple[BookDigest, str, list[AgentStep]]:
    """Runs the agent; returns the accepted digest (saved unless `dry_run`), its note and the steps it took."""
    if not dry_run:
        # Fail before spending searches and Claude calls on a digest that can't be saved.
        try:
            _recent()
        except APIError as exc:
            raise AgentError(503, f"Book Picks isn't set up on this server: {exc.message or exc}") from exc
    run = BooksAgentRun(dry_run=dry_run)
    notes = await run_agent(
        system=SYSTEM_PROMPT,
        prompt=_prompt(focus),
        tools=run.tools(),
        max_iterations=MAX_ITERATIONS,
        timeout=TIMEOUT_SECONDS,
        on_message=run.collect,
    )
    if run.digest is None:
        raise AgentError(502, "The books agent finished without an accepted digest. Please try again.")
    return run.digest, notes, run.steps
