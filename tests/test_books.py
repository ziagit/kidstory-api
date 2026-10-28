"""The books agent, the public /books reads, and sharing a digest with the social agent.

Claude is scripted at the HTTP layer as in test_agents.py. Web searches run on
Anthropic's side, so a scripted reply carries the search and its results the
way the real API returns them, and the agent's tools see what came back.
"""

import pytest
from postgrest.exceptions import APIError

from app.agents import books_agent
from app.routers import books

from .conftest import API_KEY
from .test_agents import accounts, claude, final, message, result_text, tool_results, tool_use  # noqa: F401

HEADERS = {"X-API-Key": API_KEY}

LIST_URL = "https://lists.example/childrens-bestsellers"
SHOP_URL = "https://shop.example/top-kids-books"

DIGEST = [
    "Picture books are having a lovely week on the charts. The lists this week come from a weekly "
    "children's bestseller chart and a big bookseller's top sellers, and both lean toward warm, funny "
    "stories that work well read aloud at bedtime with a small child on your lap.",
    "At the top for the youngest readers is a rhyming tale about a granny monster who loves her "
    "grandchild. It suits children aged three to five, and the bouncy rhymes make it easy to read "
    "again and again. Close behind is a cheeky book that begs you not to turn the page.",
    "For children who are starting to read on their own, a series of silly spooky stories is still "
    "selling well. The jokes carry new readers along, and the short chapters give them a sense of "
    "achievement. Older children are snapping up a spy adventure that went straight onto the list.",
    "If you're not sure where to start, pick the book that matches your child's age and sense of "
    "humour. Every one of these is gentle, funny and easy to love, and each would make a good gift.",
]

BOOKS = [
    {"title": "Gruffalo Granny", "author": "Julia Donaldson", "ages": "3-5", "why": "Bouncy rhymes for read-aloud."},
    {"title": "Do Not Open This Book", "author": "Andy Lee", "ages": "3-7", "why": "A giggly, interactive book."},
    {"title": "Spy School Goes East", "author": "Stuart Gibbs", "ages": "9-12", "why": "A funny spy adventure."},
]

SUBMISSION = {
    "title": "Picture Books Topping the Charts",
    "emoji": "📚",
    "excerpt": "Warm, funny picture books are selling fast this week.",
    "paragraphs": DIGEST,
    "books": BOOKS,
    "sources": [{"title": "Children's bestsellers", "url": LIST_URL}, {"title": "Top kids' books", "url": SHOP_URL}],
}


def searched(query: str, call_id: str, *urls: str) -> list[dict]:
    """A web search as the API returns it: the server's call, then its results."""
    return [
        {"type": "server_tool_use", "id": call_id, "name": "web_search", "input": {"query": query}},
        {
            "type": "web_search_tool_result",
            "tool_use_id": call_id,
            "content": [
                {"type": "web_search_result", "url": url, "title": url, "encrypted_content": "x", "page_age": None}
                for url in urls
            ],
        },
    ]


def test_books_agent_researches_checks_sources_and_publishes(client, claude, store):
    made_up = "https://invented.example/list"
    claude["replies"] += [
        message(tool_use("recent_digests", {}, "t1")),
        # Searches, then a submission citing a page no search returned: sent back.
        message(
            *searched("children's bestsellers this week", "srvtoolu_1", LIST_URL, SHOP_URL),
            tool_use("publish_digest", {**SUBMISSION, "sources": [{"title": "Made up", "url": made_up}]}, "t2"),
        ),
        message(tool_use("publish_digest", SUBMISSION, "t3")),
        final("I focused on picture books."),
    ]

    response = client.post("/agents/books", json={}, headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()

    rejected = result_text(tool_results(claude["requests"][2])[0])
    assert made_up in rejected and "search results" in rejected
    assert body["published"] is True
    assert body["notes"] == "I focused on picture books."
    assert [s["tool"] for s in body["steps"]] == ["recent_digests", "web_search", "publish_digest", "publish_digest"]
    assert body["steps"][1]["detail"] == "\"children's bestsellers this week\": 2 results"

    # The web search tool is declared alongside the agent's own tools.
    assert {"type": "web_search_20250305", "name": "web_search", "max_uses": 8} in claude["requests"][0]["tools"]

    digest = body["digest"]
    assert digest["slug"] == "picture-books-topping-the-charts"
    assert [b["title"] for b in digest["books"]] == [b["title"] for b in BOOKS]
    assert digest["coverImageUrl"].startswith("https://image.pollinations.ai/")
    assert store["book_digests"][0]["slug"] == digest["slug"]

    listing = client.get("/books").json()
    assert listing["total"] == 1 and listing["items"][0]["title"] == SUBMISSION["title"]
    assert "body" not in listing["items"][0]
    full = client.get(f"/books/{digest['slug']}").json()
    assert full["body"] == DIGEST and full["sources"][0]["url"] == LIST_URL


def test_books_agent_rejects_a_digest_outside_the_rules(client, claude):
    claude["replies"] += [
        message(
            *searched("kids bestsellers", "srvtoolu_1", LIST_URL, SHOP_URL),
            tool_use("publish_digest", {**SUBMISSION, "paragraphs": ["Too short."], "books": BOOKS[:1]}, "t1"),
        ),
        final("Gave up."),
    ]
    response = client.post("/agents/books", json={}, headers=HEADERS)
    assert response.status_code == 502
    problems = result_text(tool_results(claude["requests"][1])[0])
    assert "paragraphs" in problems and "Name 3 to 8 books" in problems


def test_books_agent_dry_run_saves_nothing(client, claude, store):
    claude["replies"] += [
        message(*searched("kids bestsellers", "srvtoolu_1", LIST_URL, SHOP_URL), tool_use("publish_digest", SUBMISSION, "t1")),
        final("Picture books."),
    ]
    response = client.post("/agents/books", json={"dryRun": True}, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["published"] is False
    assert store.get("book_digests", []) == []


def test_books_agent_needs_the_table_before_spending_anything(client, claude, monkeypatch):
    def missing():
        raise APIError({"message": "relation book_digests does not exist", "code": "42P01", "hint": None, "details": None})

    monkeypatch.setattr(books_agent, "_recent", missing)
    response = client.post("/agents/books", json={}, headers=HEADERS)
    assert response.status_code == 503
    assert "isn't set up" in response.json()["detail"]
    assert claude["requests"] == []


def test_books_reads_report_a_missing_table(client, monkeypatch):
    class Missing:
        def table(self, name):
            raise APIError({"message": "relation book_digests does not exist", "code": "42P01", "hint": None, "details": None})

    monkeypatch.setattr(books, "public_client", lambda: Missing())
    assert client.get("/books").status_code == 503
    assert client.get("/books/anything").status_code == 503


def test_unknown_digest_is_404(client):
    assert client.get("/books/nope").status_code == 404


def test_books_agent_requires_the_api_key(client, claude):
    assert client.post("/agents/books", json={}).status_code == 401
    assert claude["requests"] == []


def test_social_agent_shares_a_digest(client, claude, accounts, store):
    store["book_digests"] = [
        {
            "slug": "picture-books-topping-the-charts",
            "title": SUBMISSION["title"],
            "emoji": "📚",
            "excerpt": SUBMISSION["excerpt"],
            "body": DIGEST,
            "books": BOOKS,
            "sources": SUBMISSION["sources"],
            "cover_image_url": "https://img.example/cover.png",
            "published_at": "2026-10-28T09:00:00+00:00",
        }
    ]
    url = "https://kidstory.test/books/picture-books-topping-the-charts"
    claude["replies"] += [
        message(
            tool_use("post_to_facebook", {"message": "This week's best-selling picture books."}, "t1"),
            tool_use("post_to_instagram", {"caption": f"Top picture books this week. {url} #kidlit"}, "t2"),
        ),
        final("Shared the digest."),
    ]

    response = client.post(
        "/agents/social", json={"slug": "picture-books-topping-the-charts", "kind": "books"}, headers=HEADERS
    )
    assert response.status_code == 200, response.text
    assert accounts["facebook"] == [{"message": "This week's best-selling picture books.", "link": url}]
    assert accounts["instagram"][0]["image_url"] == "https://img.example/cover.png"
    prompt = claude["requests"][0]["messages"][0]["content"]
    assert "Book Picks digest" in prompt and "Gruffalo Granny by Julia Donaldson (3-5)" in prompt


def test_social_agent_404s_an_unknown_digest(client, claude, accounts):
    response = client.post("/agents/social", json={"slug": "nope", "kind": "books"}, headers=HEADERS)
    assert response.status_code == 404
    assert claude["requests"] == []


@pytest.mark.parametrize("kind", ["video"])
def test_social_agent_rejects_an_unknown_kind(client, kind):
    response = client.post("/agents/social", json={"slug": "free-story", "kind": kind}, headers=HEADERS)
    assert response.status_code == 422
