import pytest
from fastapi.testclient import TestClient

from app import partner_cli, partners, security
from app.config import get_settings
from app.agents import social_agent, story_agent
from app.routers import health, stories

from .fakes import FakeClient

API_KEY = "test-api-key"
ENTITLED_TOKEN = "token-paid"
FREE_TOKEN = "token-free"

ENTITLED_USER = "11111111-1111-1111-1111-111111111111"
FREE_USER = "22222222-2222-2222-2222-222222222222"


def _story(slug: str, *, title: str, premium: bool, published_at: str, **overrides) -> dict:
    row = {
        "slug": slug,
        "title": title,
        "emoji": "✨",
        "excerpt": f"An excerpt for {title}.",
        "category": "adventure",
        "age_range": "all-ages",
        "read_time_minutes": 2,
        "tags": ["courage"],
        "body": ["Opening paragraph.", "Middle paragraph.", "Closing paragraph."],
        "cover_image_url": None,
        "published_at": published_at,
        "is_premium": premium,
    }
    row.update(overrides)
    return row


@pytest.fixture
def store() -> dict[str, list[dict]]:
    return {
        "stories": [
            _story("free-story", title="A Free Story", premium=False, published_at="2026-01-01T00:00:00+00:00"),
            _story(
                "premium-story",
                title="A Premium Story",
                premium=True,
                published_at="2026-02-01T00:00:00+00:00",
                category="bedtime",
                age_range="3-5",
                tags=["sleepy", "moon"],
            ),
        ],
        "entitlements": [{"user_id": ENTITLED_USER}],
    }


@pytest.fixture
def client(store, monkeypatch) -> TestClient:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    monkeypatch.setenv("KIDSTORY_API_KEY", API_KEY)
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000")
    # A local .env may hold real social accounts; tests that want them say so.
    for name in ("FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ACCESS_TOKEN", "INSTAGRAM_ACCOUNT_ID", "INSTAGRAM_ACCESS_TOKEN"):
        monkeypatch.setenv(name, "")
    get_settings.cache_clear()

    fake = FakeClient(store, {ENTITLED_TOKEN: ENTITLED_USER, FREE_TOKEN: FREE_USER})
    for module in (stories, security, health, story_agent, social_agent, partners, partner_cli):
        monkeypatch.setattr(module, "public_client", lambda: fake, raising=False)
        monkeypatch.setattr(module, "admin_client", lambda: fake, raising=False)

    from app.main import create_app

    # A fresh app per test: the MCP session manager inside it can only be started once.
    with TestClient(create_app()) as test_client:
        yield test_client

    get_settings.cache_clear()
