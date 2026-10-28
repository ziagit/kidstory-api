"""The social agent: writes and publishes a post per platform for a new story or Book Picks digest.

It reads the story (or digest), checks the last few posts on each account so the new one
doesn't open the same way or reuse the same hashtags, writes copy that suits
each platform, and publishes it with that platform's tool. The link and the
image are fixed by the server. The agent only controls the words, and the
post tools check those against each platform's limits before anything is sent.
"""

import re
from dataclasses import dataclass, field

from anthropic import beta_async_tool
from anthropic.lib.tools import ToolError

from .. import social
from ..config import get_settings
from ..cover_image import build_cover_image_url
from ..schemas import AgentStep, SocialPost
from ..supabase_client import public_client
from .base import AgentError, run_agent

# Recent posts for each platform, a post each, and a retry or two.
MAX_ITERATIONS = 6
# A platform that rejects the same run's post this many times is left alone.
MAX_ATTEMPTS_PER_PLATFORM = 2
RECENT_POSTS = 5
PLATFORMS = ("facebook", "instagram")

FACEBOOK_MAX_CHARS = 1000
INSTAGRAM_MAX_CHARS = 2200  # Instagram's own caption limit
INSTAGRAM_MAX_HASHTAGS = 30  # Instagram's own hashtag limit

SYSTEM_PROMPT = """\
You share what Kidstory publishes on Kidstory's social media accounts. Kidstory \
publishes short illustrated stories for children aged 3 to 12, and a daily Book \
Picks digest on the children's books that are selling best. The people who read \
these posts are parents, grandparents and teachers.

For the story or digest you're given, write a post for each platform you have a \
tool for and publish it. Check recent_posts for that platform first, so the new post \
doesn't open the same way or reuse the same hashtags as the last few.

Facebook: one to three short sentences that make a parent want to read the story \
tonight (or, for a digest, see which books made the list). The link is attached \
for you, so don't include a URL.

Instagram: a caption of up to about 100 words: a hook, a line about the story or \
digest, its URL as plain text with a nudge to use the link in the bio (links in \
captions aren't tappable), then three to eight relevant hashtags.

Write warmly and plainly. No clickbait, no shouting in capitals, nothing the story \
or digest doesn't support, and never address children directly or ask them to do anything \
online. If a post tool returns an error, fix the post and try once more; if it \
fails again, move on. Finish with one sentence saying what you posted."""

HASHTAG = re.compile(r"(?<!\w)#\w+")


@dataclass
class SocialAgentRun:
    slug: str
    story_url: str
    image_url: str
    dry_run: bool
    platforms: list[str]
    steps: list[AgentStep] = field(default_factory=list)
    posts: dict[str, SocialPost] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)

    def _begin_post(self, platform: str) -> None:
        done = self.posts.get(platform)
        if done and done.status in ("posted", "dry_run"):
            raise ToolError(f"Already posted to {platform} this run.")
        if self.attempts.get(platform, 0) >= MAX_ATTEMPTS_PER_PLATFORM:
            raise ToolError(f"{platform} rejected this run's posts too many times; move on.")
        self.attempts[platform] = self.attempts.get(platform, 0) + 1

    async def _publish(self, platform: str, text: str, send) -> str:
        if self.dry_run:
            self.posts[platform] = SocialPost(platform=platform, status="dry_run", text=text)
            self.steps.append(AgentStep(tool=f"post_to_{platform}", detail="dry run"))
            return "Dry run: the post was recorded but not published."
        try:
            post_id = await send()
        except social.SocialError as exc:
            self.posts[platform] = SocialPost(platform=platform, status="failed", text=text, error=str(exc))
            self.steps.append(AgentStep(tool=f"post_to_{platform}", detail=f"rejected: {exc}"))
            raise ToolError(f"{platform} rejected the post: {exc}") from exc
        self.posts[platform] = SocialPost(platform=platform, status="posted", text=text, post_id=post_id)
        self.steps.append(AgentStep(tool=f"post_to_{platform}", detail=f"posted {post_id}"))
        return f"Posted to {platform}."

    def tools(self) -> list:
        settings = get_settings()

        @beta_async_tool
        async def recent_posts(platform: str) -> str:
            """The text of the latest posts on one of Kidstory's accounts, newest first.

            Args:
                platform: "facebook" or "instagram".
            """
            if platform not in self.platforms:
                raise ToolError(f"Kidstory can post to: {', '.join(self.platforms)}.")
            try:
                if platform == "facebook":
                    texts = await social.facebook_recent(
                        settings.facebook_page_id, settings.facebook_page_access_token, limit=RECENT_POSTS
                    )
                else:
                    texts = await social.instagram_recent(
                        settings.instagram_account_id, settings.instagram_access_token, limit=RECENT_POSTS
                    )
            except social.SocialError as exc:
                self.steps.append(AgentStep(tool="recent_posts", detail=f"{platform}: {exc}"))
                return f"Recent {platform} posts are unavailable ({exc}); write the post without them."
            self.steps.append(AgentStep(tool="recent_posts", detail=f"{platform}: {len(texts)} posts"))
            if not texts:
                return f"No recent {platform} posts."
            return "\n\n---\n\n".join(texts)

        @beta_async_tool
        async def post_to_facebook(message: str) -> str:
            """Publishes a post on the Kidstory Facebook Page, with the story link attached.

            Args:
                message: The post text, without a URL.
            """
            message = message.strip()
            if not message or len(message) > FACEBOOK_MAX_CHARS:
                raise ToolError(f"The message must be 1 to {FACEBOOK_MAX_CHARS} characters.")
            self._begin_post("facebook")
            return await self._publish(
                "facebook",
                message,
                lambda: social.facebook_post(
                    settings.facebook_page_id, settings.facebook_page_access_token, message=message, link=self.story_url
                ),
            )

        @beta_async_tool
        async def post_to_instagram(caption: str) -> str:
            """Publishes the story's cover image on Kidstory's Instagram with this caption.

            Args:
                caption: The caption, including the link URL as plain text and the hashtags.
            """
            caption = caption.strip()
            problems = []
            if not caption or len(caption) > INSTAGRAM_MAX_CHARS:
                problems.append(f"The caption must be 1 to {INSTAGRAM_MAX_CHARS} characters.")
            if self.story_url not in caption:
                problems.append(f"Include the URL as plain text: {self.story_url}")
            if len(HASHTAG.findall(caption)) > INSTAGRAM_MAX_HASHTAGS:
                problems.append(f"Use at most {INSTAGRAM_MAX_HASHTAGS} hashtags.")
            if problems:
                raise ToolError(" ".join(problems))
            self._begin_post("instagram")
            return await self._publish(
                "instagram",
                caption,
                lambda: social.instagram_post(
                    settings.instagram_account_id,
                    settings.instagram_access_token,
                    image_url=self.image_url,
                    caption=caption,
                ),
            )

        post_tools = {"facebook": post_to_facebook, "instagram": post_to_instagram}
        return [recent_posts, *(post_tools[p] for p in self.platforms)]

    def results(self) -> list[SocialPost]:
        out = []
        for platform in PLATFORMS:
            if platform not in self.platforms:
                out.append(SocialPost(platform=platform, status="skipped", error="Not configured on this server."))
            else:
                out.append(
                    self.posts.get(platform)
                    or SocialPost(platform=platform, status="skipped", error="The agent did not post here.")
                )
        return out


def configured_platforms() -> list[str]:
    settings = get_settings()
    platforms = []
    if settings.facebook_page_id and settings.facebook_page_access_token:
        platforms.append("facebook")
    if settings.instagram_account_id and settings.instagram_access_token:
        platforms.append("instagram")
    return platforms


def _prompt(story: dict, story_url: str, platforms: list[str]) -> str:
    body = "\n\n".join(story.get("body") or [])
    if "books" in story:
        books = "\n".join(f"- {b.get('title')} by {b.get('author')} ({b.get('ages')})" for b in story["books"] or [])
        return "\n".join(
            [
                f"Share today's Book Picks digest on: {', '.join(platforms)}.",
                f"Digest URL: {story_url}",
                f"Title: {story['title']} {story.get('emoji') or ''}".rstrip(),
                f"Excerpt: {story['excerpt']}",
                "Books it names:",
                books,
                "",
                body,
            ]
        )
    return "\n".join(
        [
            f"Share this new story on: {', '.join(platforms)}.",
            f"Story URL: {story_url}",
            f"Title: {story['title']} {story.get('emoji') or ''}".rstrip(),
            f"Category: {story['category']}; age range: {story['age_range']}",
            f"Excerpt: {story['excerpt']}",
            "",
            body,
        ]
    )


async def run_social_agent(
    *, slug: str, kind: str = "story", site_url: str | None = None, dry_run: bool = False
) -> tuple[list[SocialPost], str, list[AgentStep]]:
    """Runs the agent for one published story or (`kind="books"`) Book Picks digest; returns a result per platform."""
    table, path = ("book_digests", "books") if kind == "books" else ("stories", "stories")
    rows = public_client().table(table).select("*").eq("slug", slug).limit(1).execute().data or []
    if not rows:
        raise AgentError(404, "Digest not found." if kind == "books" else "Story not found.")
    story = rows[0]

    origin = (site_url or get_settings().site_url).rstrip("/")
    run = SocialAgentRun(
        slug=slug,
        story_url=f"{origin}/{path}/{slug}",
        image_url=story.get("cover_image_url")
        or build_cover_image_url(story["title"], story.get("category") or "children's books and reading", slug),
        dry_run=dry_run,
        platforms=configured_platforms(),
    )
    # Nothing to post to: don't spend a model call finding that out.
    if not run.platforms:
        return run.results(), "No social accounts are configured on this server.", run.steps

    notes = await run_agent(
        system=SYSTEM_PROMPT,
        prompt=_prompt(story, run.story_url, run.platforms),
        tools=run.tools(),
        max_iterations=MAX_ITERATIONS,
    )
    return run.results(), notes, run.steps
