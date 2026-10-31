"""YouTube uploads for new stories: a silent cover-image video, posted to the channel.

Ported from the Nuxt app's shared/utils/youtube.ts when the social agent took over
sharing new stories: the agent writes the title and description, and this module
renders the video and uploads it. YouTube needs OAuth 2.0: a refresh token for the
channel owner's account is exchanged for an access token on every upload (see
the Nuxt app's SOCIAL-SETUP.md and scripts/youtube-auth.mjs for getting one).

Rendering needs an ffmpeg binary. Serverless hosts (Vercel) have none, so there
the social agent simply isn't given the YouTube tool. Errors raise SocialError.
"""

import asyncio
import shutil
import tempfile
from pathlib import Path

import httpx

from .social import SocialError

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEO_SECONDS = 25
# '24' = Entertainment. Every upload is flagged made-for-kids (COPPA): it's a children's channel.
CATEGORY_ID = "24"
TIMEOUT_SECONDS = 120.0


def find_ffmpeg(configured: str = "") -> str | None:
    """The ffmpeg binary to render with: FFMPEG_PATH if set, else whatever is on PATH."""
    return configured or shutil.which("ffmpeg")


def _http() -> httpx.AsyncClient:
    # IPv4 only, for the same reason as app/social.py.
    return httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    )


async def render_cover_video(ffmpeg: str, image_url: str, workdir: Path) -> Path:
    """A silent ~25s 1080p MP4 of the cover image, letterboxed on white (no crop)."""
    async with _http() as client:
        try:
            response = await client.get(image_url, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SocialError(f"Could not fetch the cover image: {exc}") from exc
    image = workdir / "cover.jpg"
    image.write_bytes(response.content)

    video = workdir / "story.mp4"
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-loop", "1", "-i", str(image),
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=white,format=yuv420p",
        "-t", str(VIDEO_SECONDS), "-r", "25", "-c:v", "libx264", "-preset", "veryfast", str(video),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        tail = stderr.decode(errors="replace").strip().splitlines()[-1:] or ["unknown error"]
        raise SocialError(f"ffmpeg could not render the video: {tail[0]}")
    return video


async def _access_token(client: httpx.AsyncClient, client_id: str, client_secret: str, refresh_token: str) -> str:
    try:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    except httpx.HTTPError as exc:
        raise SocialError(f"Could not reach Google: {exc}") from exc
    payload = response.json() if response.content else {}
    if response.is_error or "access_token" not in payload:
        raise SocialError(f"Google refused the refresh token: {payload.get('error_description') or payload.get('error') or response.status_code}")
    return payload["access_token"]


async def upload_story_video(
    *,
    ffmpeg: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    image_url: str,
    title: str,
    description: str,
    tags: list[str],
) -> str:
    """Renders the cover video and uploads it as a public, made-for-kids video. Returns the video id."""
    with tempfile.TemporaryDirectory(prefix="kidstory-yt-") as tmp:
        video = await render_cover_video(ffmpeg, image_url, Path(tmp))
        metadata = {
            "snippet": {"title": title, "description": description, "tags": tags[:10], "categoryId": CATEGORY_ID},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": True},
        }
        async with _http() as client:
            token = await _access_token(client, client_id, client_secret, refresh_token)
            headers = {"Authorization": f"Bearer {token}"}
            try:
                # Resumable upload: one call opens the session, one sends the file.
                session = await client.post(
                    UPLOAD_URL,
                    params={"uploadType": "resumable", "part": "snippet,status"},
                    headers={**headers, "X-Upload-Content-Type": "video/mp4"},
                    json=metadata,
                )
                if session.is_error or "location" not in session.headers:
                    raise SocialError(f"YouTube would not start the upload: {_error(session)}")
                uploaded = await client.put(
                    session.headers["location"],
                    headers={**headers, "Content-Type": "video/mp4"},
                    content=video.read_bytes(),
                )
            except httpx.HTTPError as exc:
                raise SocialError(f"Could not reach YouTube: {exc}") from exc
        if uploaded.is_error:
            raise SocialError(f"YouTube rejected the video: {_error(uploaded)}")
        return str(uploaded.json().get("id", ""))


def _error(response: httpx.Response) -> str:
    try:
        return response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return f"HTTP {response.status_code}"
