"""The Graph API calls behind the social agent's post tools, against a mocked Graph API."""

import asyncio
import json

import httpx
import pytest

from app import social


@pytest.fixture
def graph(monkeypatch):
    """Scripted Graph API: maps (method, path) to a list of (status, json) replies."""
    routes: dict[tuple[str, str], list[tuple[int, dict]]] = {}
    calls: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v21.0/")
        calls.append((request.method, path, request.content))
        status, body = routes[(request.method, path)].pop(0)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(social, "_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(social, "CONTAINER_POLL_DELAY_SECONDS", 0)
    return {"routes": routes, "calls": calls}


def test_instagram_post_waits_for_the_container_then_publishes(graph):
    graph["routes"].update(
        {
            ("POST", "ig-1/media"): [(200, {"id": "container-9"})],
            ("GET", "container-9"): [(200, {"status_code": "IN_PROGRESS"}), (200, {"status_code": "FINISHED"})],
            ("POST", "ig-1/media_publish"): [(200, {"id": "media-42"})],
        }
    )
    media_id = asyncio.run(social.instagram_post("ig-1", "tok", image_url="https://img.test/a.jpg", caption="Hi"))
    assert media_id == "media-42"
    assert [(m, p) for m, p, _ in graph["calls"]] == [
        ("POST", "ig-1/media"),
        ("GET", "container-9"),
        ("GET", "container-9"),
        ("POST", "ig-1/media_publish"),
    ]
    assert json.loads(graph["calls"][-1][2])["creation_id"] == "container-9"


def test_graph_errors_carry_the_platform_message(graph):
    graph["routes"][("POST", "page-1/feed")] = [(400, {"error": {"message": "(#200) Permissions error"}})]
    with pytest.raises(social.SocialError, match=r"\(#200\) Permissions error"):
        asyncio.run(social.facebook_post("page-1", "tok", message="Hi", link="https://kidstory.test/stories/x"))


def test_recent_posts_skip_entries_without_text(graph):
    graph["routes"][("GET", "page-1/posts")] = [(200, {"data": [{"message": "One"}, {"id": "2"}, {"message": "Three"}]})]
    assert asyncio.run(social.facebook_recent("page-1", "tok")) == ["One", "Three"]
