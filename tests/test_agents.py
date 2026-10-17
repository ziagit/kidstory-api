"""The story and social agents, driven by the SDK's real tool runner.

Claude is replaced at the HTTP layer: each test scripts the assistant turns the
Messages API would return, and the tool runner runs the real tools against the
fake Supabase store in between. The requests it sends are recorded, so the
tests can check what each tool told Claude.
"""

import json

import anthropic
import httpx2
import pytest

from app import social
from app.agents import base

from .conftest import API_KEY

HEADERS = {"X-API-Key": API_KEY}

GOOD_STORY = [
    "Olive the little owl could not sleep, because the forest was far too quiet. "
    "She fluffed her feathers, blinked her big round eyes, and listened hard for any sound at all. "
    "Even the wind had gone to bed early, curled up somewhere behind the hills.",
    "So Olive hopped from branch to branch, humming a soft tune she had made up herself. "
    "A sleepy hedgehog heard her and smiled. A mouse in a mossy hole tapped his tiny foot. "
    "Soon the frogs by the pond began to croak along, slow and low, like a lullaby.",
    "By the time the moon was high, the whole forest was humming Olive's song. "
    "One by one the animals drifted off to sleep, and Olive tucked her head under her wing. "
    "The quiet did not feel lonely any more. It felt like a warm blanket, and Olive was happy, "
    "safe and sound in her tree until the morning sun came up.",
]


def tool_use(name: str, tool_input: dict, call_id: str) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}


def message(*content: dict, stop_reason: str = "tool_use") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": list(content),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def final(text: str) -> dict:
    return message({"type": "text", "text": text}, stop_reason="end_turn")


@pytest.fixture
def claude(monkeypatch):
    """Scripted Messages API. Append replies to `replies`; read sent bodies from `requests`."""
    script: dict = {"replies": [], "requests": []}

    def handler(request: httpx2.Request) -> httpx2.Response:
        script["requests"].append(json.loads(request.content))
        if not script["replies"]:
            return httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "unscripted"}})
        return httpx2.Response(200, json=script["replies"].pop(0))

    fake = anthropic.AsyncAnthropic(
        api_key="test-key", max_retries=0, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )
    monkeypatch.setattr(base, "_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    base.get_settings.cache_clear()
    return script


def tool_results(request_body: dict) -> list[dict]:
    """The tool_result blocks in the last user turn of a request."""
    return [block for block in request_body["messages"][-1]["content"] if block.get("type") == "tool_result"]


def result_text(block: dict) -> str:
    content = block["content"]
    return content if isinstance(content, str) else "".join(part.get("text", "") for part in content)


# --- story agent -----------------------------------------------------------


def test_agents_require_the_api_key(client, claude):
    assert client.post("/agents/story", json={}).status_code == 401
    assert client.post("/agents/social", json={"slug": "free-story"}).status_code == 401
    assert claude["requests"] == []


def test_agents_report_a_missing_claude_key(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    base.get_settings.cache_clear()
    response = client.post("/agents/story", json={}, headers=HEADERS)
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_story_agent_researches_revises_and_returns_a_draft(client, claude, store):
    submission = {
        "title": "Olive and the Quiet Forest",
        "emoji": "🦉",
        "excerpt": "Olive the owl can't sleep in the silent forest, so she makes a song of her own.",
        "category": "bedtime",
        "age_range": "3-5",
        "tags": ["owl", "music", "Bedtime "],
    }
    claude["replies"] += [
        message(tool_use("catalog_overview", {}, "t1"), tool_use("search_catalog", {"query": "owl"}, "t2")),
        # Too short: the tool sends it back instead of accepting it.
        message(tool_use("submit_story", {**submission, "paragraphs": ["Olive was an owl.", "She slept."]}, "t3")),
        message(tool_use("submit_story", {**submission, "paragraphs": GOOD_STORY}, "t4")),
        final("The catalog had few bedtime stories for the youngest readers."),
    ]

    response = client.post("/agents/story", json={}, headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()

    draft = body["draft"]
    assert draft["title"] == "Olive and the Quiet Forest"
    assert draft["category"] == "bedtime" and draft["ageRange"] == "3-5"
    assert draft["tags"] == ["owl", "music", "bedtime"]
    assert draft["body"] == GOOD_STORY
    assert draft["readTimeMinutes"] >= 1
    assert body["notes"] == "The catalog had few bedtime stories for the youngest readers."
    assert [s["tool"] for s in body["steps"]] == ["catalog_overview", "search_catalog", "submit_story", "submit_story"]

    # What the tools told Claude along the way.
    overview, search = tool_results(claude["requests"][1])
    stats = json.loads(result_text(overview))
    assert stats["total"] == 2
    assert stats["byCategory"]["bedtime"] == 1 and stats["byCategory"]["funny"] == 0
    assert "No stories match 'owl'" in result_text(search)

    (rejected,) = tool_results(claude["requests"][2])
    assert rejected["is_error"] is True
    assert "150 to 450 words" in result_text(rejected)

    (accepted,) = tool_results(claude["requests"][3])
    assert result_text(accepted) == "Accepted."

    # The prompt left the choices to the agent.
    assert "Choose the premise yourself." in claude["requests"][0]["messages"][0]["content"]

    # The draft publishes as-is.
    created = client.post("/stories", json=draft, headers=HEADERS)
    assert created.status_code == 201, created.text


def test_story_agent_holds_fixed_choices_and_unique_titles(client, claude):
    claude["replies"] += [
        message(
            tool_use(
                "submit_story",
                {
                    "title": "A Free Story",  # already in the store
                    "emoji": "✨",
                    "excerpt": "An excerpt.",
                    "category": "funny",
                    "age_range": "6-8",
                    "tags": ["fun"],
                    "paragraphs": GOOD_STORY,
                },
                "t1",
            )
        ),
        final("I could not finish."),
    ]

    response = client.post(
        "/agents/story", json={"topic": "a brave kite", "category": "adventure", "ageRange": "9-12"}, headers=HEADERS
    )
    # The agent gave up without an accepted story.
    assert response.status_code == 502
    assert "without an accepted story" in response.json()["detail"]

    prompt = claude["requests"][0]["messages"][0]["content"]
    assert "Premise to build on: a brave kite" in prompt
    assert "Category (fixed): adventure" in prompt and "Age range (fixed): 9-12" in prompt

    problems = result_text(tool_results(claude["requests"][1])[0])
    assert "'A Free Story' already exists" in problems
    assert "category is fixed to 'adventure'" in problems
    assert "age range is fixed to '9-12'" in problems


def test_story_agent_surfaces_a_refusal(client, claude):
    claude["replies"] += [message({"type": "text", "text": ""}, stop_reason="refusal")]
    response = client.post("/agents/story", json={}, headers=HEADERS)
    assert response.status_code == 422


# --- social agent ----------------------------------------------------------


@pytest.fixture
def accounts(monkeypatch):
    """Configures both platforms and replaces the Graph API with recorders."""
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "page-1")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "fb-token")
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "ig-1")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "ig-token")
    monkeypatch.setenv("SITE_URL", "https://kidstory.test")
    base.get_settings.cache_clear()

    sent: dict[str, list] = {"facebook": [], "instagram": []}

    async def facebook_recent(page_id, token, *, limit):
        return ["Tonight's story is about a sleepy bear. 🐻"]

    async def instagram_recent(account_id, token, *, limit):
        return ["A new story! #bedtimestories #kidlit"]

    async def facebook_post(page_id, token, *, message, link):
        sent["facebook"].append({"message": message, "link": link})
        return "fb-post-1"

    async def instagram_post(account_id, token, *, image_url, caption):
        sent["instagram"].append({"image_url": image_url, "caption": caption})
        return "ig-media-1"

    for name, fn in [
        ("facebook_recent", facebook_recent),
        ("instagram_recent", instagram_recent),
        ("facebook_post", facebook_post),
        ("instagram_post", instagram_post),
    ]:
        monkeypatch.setattr(social, name, fn)
    return sent


def test_social_agent_writes_checks_and_posts(client, claude, accounts):
    url = "https://kidstory.test/stories/free-story"
    claude["replies"] += [
        message(
            tool_use("recent_posts", {"platform": "facebook"}, "t1"),
            tool_use("recent_posts", {"platform": "instagram"}, "t2"),
        ),
        # The Instagram caption forgot the link; the tool sends it back.
        message(
            tool_use("post_to_facebook", {"message": "A gentle story for tonight."}, "t3"),
            tool_use("post_to_instagram", {"caption": "A gentle story. #storytime"}, "t4"),
        ),
        message(tool_use("post_to_instagram", {"caption": f"A gentle story.\n\nRead it: {url} (link in bio)\n\n#storytime"}, "t5")),
        final("Posted to Facebook and Instagram."),
    ]

    response = client.post("/agents/social", json={"slug": "free-story"}, headers=HEADERS)
    assert response.status_code == 200, response.text
    posts = {p["platform"]: p for p in response.json()["posts"]}

    assert posts["facebook"]["status"] == "posted" and posts["facebook"]["postId"] == "fb-post-1"
    assert posts["instagram"]["status"] == "posted" and posts["instagram"]["postId"] == "ig-media-1"
    assert accounts["facebook"] == [{"message": "A gentle story for tonight.", "link": url}]
    assert len(accounts["instagram"]) == 1
    assert url in accounts["instagram"][0]["caption"]
    assert accounts["instagram"][0]["image_url"].startswith("https://image.pollinations.ai/")

    # The story itself reached the agent, and recent posts came back from the tools.
    prompt = claude["requests"][0]["messages"][0]["content"]
    assert "Title: A Free Story" in prompt and f"Story URL: {url}" in prompt
    fb_recent, ig_recent = tool_results(claude["requests"][1])
    assert "sleepy bear" in result_text(fb_recent)
    assert "#kidlit" in result_text(ig_recent)
    _, ig_rejected = tool_results(claude["requests"][2])
    assert ig_rejected["is_error"] is True and url in result_text(ig_rejected)


def test_social_agent_dry_run_publishes_nothing(client, claude, accounts):
    claude["replies"] += [
        message(tool_use("post_to_facebook", {"message": "A gentle story for tonight."}, "t1")),
        message(tool_use("post_to_facebook", {"message": "Posting again."}, "t2")),
        final("Wrote a Facebook post."),
    ]
    response = client.post("/agents/social", json={"slug": "free-story", "dryRun": True}, headers=HEADERS)
    assert response.status_code == 200, response.text
    posts = {p["platform"]: p for p in response.json()["posts"]}

    assert posts["facebook"] == {
        "platform": "facebook",
        "status": "dry_run",
        "text": "A gentle story for tonight.",
        "postId": None,
        "error": None,
    }
    assert posts["instagram"]["status"] == "skipped"
    assert accounts == {"facebook": [], "instagram": []}
    # A second post to the same platform is refused.
    (again,) = tool_results(claude["requests"][2])
    assert again["is_error"] is True and "Already posted" in result_text(again)


def test_social_agent_reports_a_rejected_post(client, claude, accounts, monkeypatch):
    async def rejecting(page_id, token, *, message, link):
        raise social.SocialError("(#200) Permissions error")

    monkeypatch.setattr(social, "facebook_post", rejecting)
    claude["replies"] += [
        message(tool_use("post_to_facebook", {"message": "One."}, "t1")),
        message(tool_use("post_to_facebook", {"message": "Two."}, "t2")),
        message(tool_use("post_to_facebook", {"message": "Three."}, "t3")),
        final("Facebook would not take the post."),
    ]
    response = client.post("/agents/social", json={"slug": "free-story"}, headers=HEADERS)
    assert response.status_code == 200, response.text
    facebook = next(p for p in response.json()["posts"] if p["platform"] == "facebook")
    assert facebook["status"] == "failed" and facebook["error"] == "(#200) Permissions error"
    # Two attempts reach Facebook; the third is stopped before it does.
    (third,) = tool_results(claude["requests"][3])
    assert "too many times" in result_text(third)


def test_social_agent_skips_the_model_with_no_accounts(client, claude):
    response = client.post("/agents/social", json={"slug": "free-story"}, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert {p["status"] for p in response.json()["posts"]} == {"skipped"}
    assert claude["requests"] == []


def test_social_agent_needs_a_published_story(client, claude, accounts):
    response = client.post("/agents/social", json={"slug": "no-such-story"}, headers=HEADERS)
    assert response.status_code == 404
