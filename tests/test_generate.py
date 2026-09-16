import pytest

from app import story_generator
from app.story_generator import draft_from_text

from .conftest import API_KEY

STORY = (
    "Once upon a time, a small grey mouse named Pip lived under the old barn. "
    "Every night he watched the farm cat, Whiskers, prowl past his door. Pip was afraid, "
    "but he was also curious.\n\n"
    "One evening Whiskers got her paw stuck in the fence. Pip took a deep breath and "
    "crept out to help. He nibbled the string until the cat was free.\n\n"
    "From that night on, Whiskers and Pip shared the barn together. They were the best of friends."
)


@pytest.fixture
def fake_model(monkeypatch):
    """Stands in for the model server; records the messages it was sent."""
    calls: list[list[dict[str, str]]] = []
    replies: list[str] = [STORY]

    async def _complete(messages):
        calls.append(messages)
        return replies.pop(0) if replies else STORY

    monkeypatch.setattr(story_generator, "_complete", _complete)
    return {"calls": calls, "replies": replies}


def test_generate_requires_the_api_key(client, fake_model):
    response = client.post("/stories/generate", json={"topic": "a cat and a mouse"})
    assert response.status_code == 401
    assert fake_model["calls"] == []


def test_generate_returns_a_publishable_draft(client, fake_model):
    response = client.post(
        "/stories/generate",
        json={"topic": "Write a story about a cat and a mouse who become friends", "ageRange": "3-5"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 200, response.text
    draft = response.json()["draft"]

    assert draft["title"] == "A Cat and a Mouse Who Become Friends"
    assert draft["body"][0].startswith("Once upon a time, a small grey mouse named Pip")
    assert len(draft["body"]) == 3
    assert draft["excerpt"] == "Once upon a time, a small grey mouse named Pip lived under the old barn."
    assert draft["emoji"] == "🐱"
    assert draft["category"] == "animals"  # inferred: no category asked for, animals in the text
    assert draft["ageRange"] == "3-5"
    assert draft["readTimeMinutes"] == 1
    assert 2 <= len(draft["tags"]) <= 3 and "cat" in draft["tags"] and "mouse" in draft["tags"]

    # The draft round-trips straight into POST /stories.
    created = client.post("/stories", json=draft, headers={"X-API-Key": API_KEY})
    assert created.status_code == 201, created.text
    assert created.json()["slug"] == "a-cat-and-a-mouse-who-become-friends"


def test_generate_prompt_carries_hints_and_history(client, fake_model):
    history = [
        {"role": "user", "content": "Write a short story about a kite."},
        {"role": "assistant", "content": "A kite flew.\n\nIt landed."},
    ]
    response = client.post(
        "/stories/generate",
        json={"topic": "make it about a brave kite", "category": "adventure", "ageRange": "6-8", "history": history},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 200, response.text

    messages = fake_model["calls"][0]
    assert messages[0]["role"] == "system"
    assert [m["content"] for m in messages[1:3]] == [h["content"] for h in history]
    assert messages[-1] == {
        "role": "user",
        "content": "make it about a brave kite. Make it an adventure story. It is for children aged 6-8.",
    }

    # A revision keeps the title of the story being revised, not "Make It About a Brave Kite".
    assert response.json()["draft"]["title"] == "A Kite"

    # The returned history includes this turn, ready to be sent back.
    returned = response.json()["history"]
    assert [m["role"] for m in returned] == ["user", "assistant", "user", "assistant"]
    assert returned[-1]["content"] == STORY
    assert response.json()["draft"]["category"] == "adventure"


def test_generate_retries_once_on_an_empty_reply(client, fake_model):
    fake_model["replies"][:] = ["", STORY]
    response = client.post("/stories/generate", json={"topic": "a mouse"}, headers={"X-API-Key": API_KEY})
    assert response.status_code == 200
    assert len(fake_model["calls"]) == 2


def test_generate_gives_up_after_two_empty_replies(client, fake_model):
    fake_model["replies"][:] = ["", "   "]
    response = client.post("/stories/generate", json={"topic": "a mouse"}, headers={"X-API-Key": API_KEY})
    assert response.status_code == 502
    assert "empty story" in response.json()["detail"]


def test_generate_is_503_when_no_model_is_configured(client, monkeypatch):
    monkeypatch.setenv("STORY_MODEL_URL", "")
    story_generator.get_settings.cache_clear()
    response = client.post("/stories/generate", json={"topic": "a mouse"}, headers={"X-API-Key": API_KEY})
    assert response.status_code == 503
    assert "STORY_MODEL_URL" in response.json()["detail"]


def test_a_leading_title_line_is_lifted_out_of_the_body():
    draft = draft_from_text(
        "Title: The Moonlit Pond\n\nA frog sat by the pond. He looked at the moon.",
        topic="a frog",
        category=None,
        age_range=None,
    )
    assert draft.title == "The Moonlit Pond"
    assert draft.body == ["A frog sat by the pond. He looked at the moon."]
    assert draft.emoji == "🐸"
    assert draft.category == "animals"


def test_a_single_block_of_prose_is_split_into_paragraphs():
    sentences = " ".join(f"Sentence number {i} is here." for i in range(1, 10))
    draft = draft_from_text(sentences, topic="numbers", category="funny", age_range="9-12")
    assert len(draft.body) == 3
    assert " ".join(draft.body) == sentences
    assert draft.title == "Numbers"
    assert draft.emoji == "😄"
    assert draft.tags == ["funny"]


def test_topics_with_instruction_phrasing_become_clean_titles():
    draft = draft_from_text("A story.", topic="Please write me a short bedtime story about the little owl who couldn't sleep", category="bedtime", age_range=None)
    assert draft.title == "The Little Owl Who Couldn't Sleep"
    assert draft.emoji == "🦉"
