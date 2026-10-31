"""The owner's new-story email and the YouTube upload, with SMTP and Google mocked."""

import asyncio
import json

import httpx
import pytest

from app import notify, youtube
from app.social import SocialError

STORY = {
    "title": "Olive <& the Fox>",
    "emoji": "🦊",
    "excerpt": "A fox learns to share.",
    "category": "animals",
    "age_range": "3-5",
    "read_time_minutes": 2,
    "body": ["Once upon a time.", "The end."],
}


def test_story_email_html_escapes_and_includes_the_story():
    html = notify.story_email_html(STORY, "https://kidstory.test/stories/olive", "https://img.test/c.jpg", "A sweet one.")
    assert "Olive &lt;&amp; the Fox&gt;" in html and "<& the" not in html
    assert "<p>Once upon a time.</p>" in html and "<p>A sweet one.</p>" in html
    assert 'href="https://kidstory.test/stories/olive"' in html


def test_send_email_reports_smtp_failures(monkeypatch):
    class Refusing:
        def __init__(self, *args, **kwargs):
            raise OSError("Network is unreachable")

    monkeypatch.setattr(notify, "_SMTP4", Refusing)
    with pytest.raises(SocialError, match="Network is unreachable"):
        asyncio.run(notify.send_email("me@x.test", "pw", to=["me@x.test"], subject="Hi", html="<p>Hi</p>", text="Hi"))


@pytest.fixture
def google(monkeypatch, tmp_path):
    calls: list[tuple[str, str, bytes, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.content, dict(request.headers)))
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access-1"})
        if request.method == "POST":
            return httpx.Response(200, headers={"location": "https://upload.test/session-1"})
        return httpx.Response(200, json={"id": "video-9"})

    async def render(ffmpeg, image_url, workdir):
        video = workdir / "story.mp4"
        video.write_bytes(b"mp4-bytes")
        return video

    monkeypatch.setattr(youtube, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(youtube, "render_cover_video", render)
    return calls


def test_upload_story_video_refreshes_the_token_then_uploads(google):
    video_id = asyncio.run(
        youtube.upload_story_video(
            ffmpeg="/usr/bin/ffmpeg",
            client_id="cid",
            client_secret="secret",
            refresh_token="refresh",
            image_url="https://img.test/c.jpg",
            title="Olive and the Fox 🦊",
            description="A story. https://kidstory.test/stories/olive",
            tags=[f"tag{i}" for i in range(12)],
        )
    )
    assert video_id == "video-9"
    token_call, session_call, upload_call = google
    assert b"grant_type=refresh_token" in token_call[2]
    metadata = json.loads(session_call[2])
    assert metadata["status"] == {"privacyStatus": "public", "selfDeclaredMadeForKids": True}
    assert len(metadata["snippet"]["tags"]) == 10
    assert session_call[3]["authorization"] == "Bearer access-1"
    assert upload_call[0] == "PUT" and upload_call[1] == "https://upload.test/session-1" and upload_call[2] == b"mp4-bytes"


def test_upload_story_video_reports_a_refused_refresh_token(monkeypatch, google):
    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})

    monkeypatch.setattr(youtube, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(refusing)))
    with pytest.raises(SocialError, match="expired or revoked"):
        asyncio.run(
            youtube.upload_story_video(
                ffmpeg="ffmpeg", client_id="c", client_secret="s", refresh_token="r",
                image_url="https://img.test/c.jpg", title="T", description="D", tags=[],
            )
        )
