from .conftest import API_KEY, READER_TOKEN


def test_health_reports_database_reachable(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_categories_are_the_six_site_categories(client):
    slugs = [category["slug"] for category in client.get("/categories").json()]
    assert slugs == ["adventure", "bedtime", "animals", "friendship", "fairy-tale", "funny"]


def test_list_returns_camel_case_summaries_newest_first_without_body(client):
    payload = client.get("/stories").json()
    assert payload["total"] == 2
    assert [item["slug"] for item in payload["items"]] == ["bedtime-story", "free-story"]
    first = payload["items"][0]
    assert "body" not in first
    assert first["ageRange"] == "3-5"
    assert first["readTimeMinutes"] == 2


def test_list_filters_by_category_age_tag_and_search(client):
    assert client.get("/stories", params={"category": "bedtime"}).json()["total"] == 1
    assert client.get("/stories", params={"ageRange": "3-5"}).json()["total"] == 1
    assert client.get("/stories", params={"tag": "moon"}).json()["total"] == 1
    assert client.get("/stories", params={"q": "bedtime"}).json()["total"] == 1
    assert client.get("/stories", params={"q": "nothing here"}).json()["total"] == 0


def test_list_rejects_an_unknown_category(client):
    assert client.get("/stories", params={"category": "spooky"}).status_code == 400


def test_every_story_returns_its_full_body_to_anyone(client):
    for slug in ("free-story", "bedtime-story"):
        payload = client.get(f"/stories/{slug}").json()
        assert len(payload["body"]) == 3
        assert "locked" not in payload


def test_signed_in_and_invalid_tokens_get_the_same_full_story(client):
    for token in (READER_TOKEN, "garbage"):
        response = client.get("/stories/bedtime-story", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert len(response.json()["body"]) == 3


def test_missing_story_is_404(client):
    assert client.get("/stories/no-such-story").status_code == 404


def test_create_requires_the_api_key(client):
    response = client.post("/stories", json={"title": "T", "excerpt": "E", "body": ["P"]})
    assert response.status_code == 401


def test_create_derives_slug_and_cover(client):
    response = client.post(
        "/stories",
        headers={"X-API-Key": API_KEY},
        json={
            "title": "The Brave Little Kite!",
            "excerpt": "A kite learns to fly.",
            "body": ["One.", "  ", "Two."],
            "category": "adventure",
            "ageRange": "6-8",
            "tags": ["courage", "sky"],
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["slug"] == "the-brave-little-kite"
    assert "isPremium" not in payload
    assert payload["body"] == ["One.", "Two."]  # blank paragraphs dropped
    assert payload["coverImageUrl"].startswith("https://image.pollinations.ai/prompt/")


def test_create_appends_a_suffix_when_the_derived_slug_is_taken(client):
    # "Free Story" slugifies to "free-story", which the fixture already holds.
    body = {"title": "Free Story", "excerpt": "Another one.", "body": ["Text."]}
    response = client.post("/stories", headers={"X-API-Key": API_KEY}, json=body)
    assert response.status_code == 201
    assert response.json()["slug"] == "free-story-2"


def test_create_conflicts_when_an_explicit_slug_is_taken(client):
    response = client.post(
        "/stories",
        headers={"X-API-Key": API_KEY},
        json={"title": "Anything", "excerpt": "E", "body": ["P"], "slug": "free-story"},
    )
    assert response.status_code == 409


def test_create_rejects_an_unknown_category(client):
    response = client.post(
        "/stories",
        headers={"X-API-Key": API_KEY},
        json={"title": "T", "excerpt": "E", "body": ["P"], "category": "spooky"},
    )
    assert response.status_code == 422


def test_create_rejects_an_empty_body(client):
    response = client.post(
        "/stories",
        headers={"X-API-Key": API_KEY},
        json={"title": "T", "excerpt": "E", "body": ["   "]},
    )
    assert response.status_code == 422


def test_update_writes_only_the_supplied_fields(client):
    response = client.patch(
        "/stories/free-story",
        headers={"X-API-Key": API_KEY},
        json={"title": "A Renamed Story", "emoji": "🌙"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "A Renamed Story"
    assert payload["emoji"] == "🌙"
    assert payload["excerpt"] == "An excerpt for A Free Story."  # untouched


def test_update_rejects_an_empty_payload(client):
    response = client.patch("/stories/free-story", headers={"X-API-Key": API_KEY}, json={})
    assert response.status_code == 400


def test_update_missing_story_is_404(client):
    response = client.patch("/stories/nope", headers={"X-API-Key": API_KEY}, json={"title": "X"})
    assert response.status_code == 404


def test_delete_removes_the_story(client):
    assert client.delete("/stories/free-story", headers={"X-API-Key": API_KEY}).status_code == 204
    assert client.get("/stories/free-story").status_code == 404
    assert client.delete("/stories/free-story", headers={"X-API-Key": API_KEY}).status_code == 404


def test_delete_requires_the_api_key(client):
    assert client.delete("/stories/free-story").status_code == 401
