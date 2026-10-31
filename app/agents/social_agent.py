"""The social agent: shares each new story or Book Picks digest everywhere it goes.

It reads the story (or digest), checks the last few posts on each account so the new one
doesn't open the same way or reuse the same hashtags, writes copy that suits
each platform, and publishes it with that platform's tool. For a new story it
also emails the owner a copy and uploads a cover video to YouTube. The link,
image, story text and video are fixed by the server. The agent only controls the
words, and each tool checks those against the platform's limits before anything is sent.
"""

import re
from dataclasses import dataclass, field

from anthropic import beta_async_tool
from anthropic.lib.tools import ToolError

from .. import activity, notify, social, youtube
from ..config import get_settings
from ..cover_image import build_cover_image_url
from ..schemas import AgentStep, SocialPost
from ..supabase_client import public_client
from .base import AgentError, run_agent

# Recent posts for each platform, a post (or email, or upload) each, and a retry or two.
MAX_ITERATIONS = 10
# A platform that rejects the same run's post this many times is left alone.
MAX_ATTEMPTS_PER_PLATFORM = 2
RECENT_POSTS = 5
PLATFORMS = ("facebook", "instagram", "email", "youtube")
# Platforms with a public feed recent_posts can read.
FEEDS = ("facebook", "instagram")

FACEBOOK_MAX_CHARS = 1000
INSTAGRAM_MAX_CHARS = 2200  # Instagram's own caption limit
INSTAGRAM_MAX_HASHTAGS = 30  # Instagram's own hashtag limit
EMAIL_SUBJECT_MAX_CHARS = 150
EMAIL_NOTE_MAX_CHARS = 600
YOUTUBE_TITLE_MAX_CHARS = 100  # YouTube's own limits
YOUTUBE_DESCRIPTION_MAX_CHARS = 5000

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

Email (new stories only): this goes to Kidstory's owner, not to readers. Write a \
subject naming the story and a short note of one to three sentences: what the \
story is about and who it suits. The cover, the full story and its link are added \
for you.

YouTube (new stories only): the video is the story's cover image. Write a title \
of up to 100 characters (the story title plus its emoji works well) and a \
description: a line or two about the story for parents, then its URL so viewers \
can read it. The channel is for children, so keep it gentle and never ask viewers \
to comment, subscribe or click anything other than the story link.

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
    story: dict = field(default_factory=dict)
    # Why each platform that isn't in `platforms` was left out.
    unavailable: dict[str, str] = field(default_factory=dict)
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

    async def _publish(self, platform: str, text: str, send, tool: str | None = None) -> str:
        tool = tool or f"post_to_{platform}"
        if self.dry_run:
            self.posts[platform] = SocialPost(platform=platform, status="dry_run", text=text)
            self.steps.append(AgentStep(tool=tool, detail="dry run"))
            return "Dry run: recorded but not sent."
        try:
            post_id = await send()
        except social.SocialError as exc:
            self.posts[platform] = SocialPost(platform=platform, status="failed", text=text, error=str(exc))
            self.steps.append(AgentStep(tool=tool, detail=f"rejected: {exc}"))
            raise ToolError(f"{platform} rejected it: {exc}") from exc
        self.posts[platform] = SocialPost(platform=platform, status="posted", text=text, post_id=post_id)
        self.steps.append(AgentStep(tool=tool, detail=f"posted {post_id}"))
        return f"Done: sent to {platform}."

    def tools(self) -> list:
        settings = get_settings()

        @beta_async_tool
        async def recent_posts(platform: str) -> str:
            """The text of the latest posts on one of Kidstory's accounts, newest first.

            Args:
                platform: "facebook" or "instagram".
            """
            feeds = [p for p in self.platforms if p in FEEDS]
            if platform not in feeds:
                raise ToolError(f"Recent posts can be read from: {', '.join(feeds) or 'none'}.")
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

        @beta_async_tool
        async def email_owner(subject: str, note: str) -> str:
            """Emails Kidstory's owner a copy of the new story: your subject and note, then the
            cover, the full story and its link.

            Args:
                subject: The email subject, naming the story.
                note: One to three sentences for the owner about the story.
            """
            subject, note = subject.strip(), note.strip()
            problems = []
            if not subject or len(subject) > EMAIL_SUBJECT_MAX_CHARS or "\n" in subject:
                problems.append(f"The subject must be one line of 1 to {EMAIL_SUBJECT_MAX_CHARS} characters.")
            if not note or len(note) > EMAIL_NOTE_MAX_CHARS:
                problems.append(f"The note must be 1 to {EMAIL_NOTE_MAX_CHARS} characters.")
            if problems:
                raise ToolError(" ".join(problems))
            self._begin_post("email")
            html = notify.story_email_html(self.story, self.story_url, self.image_url, note)
            text = f"{note}\n\n{self.story['title']}\n\n" + "\n\n".join(self.story.get("body") or []) + f"\n\nRead it live: {self.story_url}"
            return await self._publish(
                "email",
                f"{subject}\n\n{note}",
                lambda: notify.send_email(
                    settings.gmail_user,
                    settings.gmail_app_password,
                    to=settings.notify_email_list(),
                    subject=subject,
                    html=html,
                    text=text,
                ),
                tool="email_owner",
            )

        @beta_async_tool
        async def post_to_youtube(title: str, description: str) -> str:
            """Uploads a short video of the story's cover image to Kidstory's YouTube channel,
            public and marked made for kids. The story's tags are added for you.

            Args:
                title: The video title, up to 100 characters.
                description: A line or two for parents, then the story URL.
            """
            title, description = title.strip(), description.strip()
            problems = []
            if not title or len(title) > YOUTUBE_TITLE_MAX_CHARS or "<" in title or ">" in title:
                problems.append(f"The title must be 1 to {YOUTUBE_TITLE_MAX_CHARS} characters, without < or >.")
            if not description or len(description) > YOUTUBE_DESCRIPTION_MAX_CHARS or "<" in description or ">" in description:
                problems.append(f"The description must be 1 to {YOUTUBE_DESCRIPTION_MAX_CHARS} characters, without < or >.")
            if self.story_url not in description:
                problems.append(f"Include the story URL in the description: {self.story_url}")
            if problems:
                raise ToolError(" ".join(problems))
            self._begin_post("youtube")
            return await self._publish(
                "youtube",
                f"{title}\n\n{description}",
                lambda: youtube.upload_story_video(
                    ffmpeg=youtube.find_ffmpeg(settings.ffmpeg_path) or "",
                    client_id=settings.youtube_client_id,
                    client_secret=settings.youtube_client_secret,
                    refresh_token=settings.youtube_refresh_token,
                    image_url=self.image_url,
                    title=title,
                    description=description,
                    tags=list(self.story.get("tags") or []),
                ),
            )

        post_tools = {
            "facebook": post_to_facebook,
            "instagram": post_to_instagram,
            "email": email_owner,
            "youtube": post_to_youtube,
        }
        feeds = [recent_posts] if any(p in FEEDS for p in self.platforms) else []
        return [*feeds, *(post_tools[p] for p in self.platforms)]

    def results(self) -> list[SocialPost]:
        out = []
        for platform in PLATFORMS:
            if platform not in self.platforms:
                reason = self.unavailable.get(platform, "Not configured on this server.")
                out.append(SocialPost(platform=platform, status="skipped", error=reason))
            else:
                out.append(
                    self.posts.get(platform)
                    or SocialPost(platform=platform, status="skipped", error="The agent did not post here.")
                )
        return out


def available_platforms(kind: str = "story") -> tuple[list[str], dict[str, str]]:
    """The platforms this run can share to, and why each of the others can't."""
    settings = get_settings()
    platforms: list[str] = []
    unavailable: dict[str, str] = {}

    def offer(platform: str, configured: bool, reason: str = "Not configured on this server.") -> None:
        if configured:
            platforms.append(platform)
        else:
            unavailable[platform] = reason

    offer("facebook", bool(settings.facebook_page_id and settings.facebook_page_access_token))
    offer("instagram", bool(settings.instagram_account_id and settings.instagram_access_token))
    if kind != "story":
        unavailable["email"] = unavailable["youtube"] = "Only new stories are emailed and uploaded."
        return platforms, unavailable
    offer("email", bool(settings.gmail_user and settings.gmail_app_password))
    if not (settings.youtube_client_id and settings.youtube_client_secret and settings.youtube_refresh_token):
        unavailable["youtube"] = "Not configured on this server."
    else:
        offer("youtube", bool(youtube.find_ffmpeg(settings.ffmpeg_path)), "This server has no ffmpeg to render the video.")
    return platforms, unavailable


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
    """Runs the agent and records the run, with the platforms it posted to."""
    with activity.tracked("social", kind=kind, slug=slug, dry_run=dry_run or None) as log:
        posts, notes, steps = await _run_social_agent(slug=slug, kind=kind, site_url=site_url, dry_run=dry_run)
        log.detail["platforms"] = [p.platform for p in posts if p.status == "posted"]
        return posts, notes, steps


async def _run_social_agent(
    *, slug: str, kind: str, site_url: str | None, dry_run: bool
) -> tuple[list[SocialPost], str, list[AgentStep]]:
    """Runs the agent for one published story or (`kind="books"`) Book Picks digest; returns a result per platform."""
    table, path = ("book_digests", "books") if kind == "books" else ("stories", "stories")
    rows = public_client().table(table).select("*").eq("slug", slug).limit(1).execute().data or []
    if not rows:
        raise AgentError(404, "Digest not found." if kind == "books" else "Story not found.")
    story = rows[0]

    origin = (site_url or get_settings().site_url).rstrip("/")
    platforms, unavailable = available_platforms(kind)
    run = SocialAgentRun(
        slug=slug,
        story_url=f"{origin}/{path}/{slug}",
        image_url=story.get("cover_image_url")
        or build_cover_image_url(story["title"], story.get("category") or "children's books and reading", slug),
        dry_run=dry_run,
        platforms=platforms,
        story=story,
        unavailable=unavailable,
    )
    # Nothing to post to: don't spend a model call finding that out.
    if not run.platforms:
        return run.results(), "Nothing to share to is configured on this server.", run.steps

    notes = await run_agent(
        system=SYSTEM_PROMPT,
        prompt=_prompt(story, run.story_url, run.platforms),
        tools=run.tools(),
        max_iterations=MAX_ITERATIONS,
    )
    return run.results(), notes, run.steps
