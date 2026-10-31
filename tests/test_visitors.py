"""Visitor stats (GET /visitors/stats) and the visitors agent (POST /agents/visitors)."""

from datetime import datetime, timedelta, timezone

import pytest

from app import visitors
from app.agents import visitors_agent

from .conftest import API_KEY
from .fakes import FakeClient
from .test_agents import claude, final, message, result_text, tool_results, tool_use  # noqa: F401

HEADERS = {"X-API-Key": API_KEY}
NOW = datetime.now(timezone.utc)
ALICE, BOB = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"


def _visit(visitor: str, *, ago: timedelta, seconds: int, pages: list[tuple[str, int]], is_new=False, **extra) -> dict:
    started = NOW - ago
    return {
        "visitor_id": visitor,
        "is_new": is_new,
        "started_at": started.isoformat(),
        "last_seen_at": (started + timedelta(seconds=seconds)).isoformat(),
        "active_seconds": seconds,
        "page_views": len(pages),
        "pages": [{"path": p, "seconds": s} for p, s in pages],
        "entry_path": pages[0][0],
        "referrer": extra.get("referrer"),
        "device": extra.get("device", "mobile"),
        "country": extra.get("country", "PK"),
    }


@pytest.fixture
def visits(store, client, monkeypatch):
    store["site_visits"] = [
        _visit(ALICE, ago=timedelta(hours=1), seconds=180, pages=[("/", 20), ("/stories/free-story", 160)], is_new=True, referrer="facebook.com"),
        _visit(ALICE, ago=timedelta(minutes=1), seconds=60, pages=[("/stories/bedtime-story", 60)]),
        _visit(BOB, ago=timedelta(hours=2), seconds=5, pages=[("/", 5)], is_new=True, device="desktop"),
        # Outside a 7-day window.
        _visit(BOB, ago=timedelta(days=20), seconds=300, pages=[("/stories/free-story", 300)]),
    ]
    rows = store["site_visits"]
    store["site_visit_totals"] = [
        {
            "visitors": 2,
            "visits": len(rows),
            "page_views": sum(r["page_views"] for r in rows),
            "active_seconds": sum(r["active_seconds"] for r in rows),
            "first_visit_at": rows[-1]["started_at"],
        }
    ]
    fake = FakeClient(store, {})
    monkeypatch.setattr(visitors, "admin_client", lambda: fake)
    monkeypatch.setattr(visitors_agent, "public_client", lambda: fake)
    return rows


def test_stats_need_the_api_key(client, visits):
    assert client.get("/visitors/stats").status_code == 401


def test_stats_count_visitors_and_time_on_the_site(client, visits):
    response = client.get("/visitors/stats", params={"days": 7}, headers=HEADERS)
    assert response.status_code == 200, response.text
    stats = response.json()

    assert stats["liveNow"] == 1
    assert stats["allTime"]["visitors"] == 2 and stats["allTime"]["visits"] == 4
    assert stats["allTime"]["avgVisitSeconds"] == round((180 + 60 + 5 + 300) / 4)

    period = stats["period"]
    assert period["visitors"] == 2 and period["visits"] == 3 and period["newVisitors"] == 2
    assert period["totalSeconds"] == 245 and period["avgVisitSeconds"] == 82 and period["medianVisitSeconds"] == 60
    assert period["avgSecondsPerVisitor"] == round(245 / 2)
    # Bob's 5-second, one-page visit bounced.
    assert period["bounceRate"] == round(1 / 3, 3)
    assert len(period["daily"]) == 7 and sum(d["visits"] for d in period["daily"]) == 3
    assert period["topPages"][0] == {"path": "/", "visits": 2, "seconds": 25, "avgSeconds": 12}
    assert {"name": "facebook.com", "visits": 1} in period["referrers"]
    assert period["devices"][0] == {"name": "mobile", "visits": 2}


def test_compare_periods_splits_the_window(visits):
    result = visitors.compare_periods(15)
    # Bob's visit 20 days ago falls in the 15 days before.
    assert result["current"]["visits"] == 3 and result["previous"]["visits"] == 1
    assert result["previous"]["top_pages"] == [{"path": "/stories/free-story", "visits": 1, "seconds": 300, "avg_seconds": 300}]
    result = visitors.compare_periods(7)
    assert result["current"]["visits"] == 3 and result["previous"]["visits"] == 0


def test_stats_report_a_missing_table(client, monkeypatch):
    from postgrest.exceptions import APIError

    def missing():
        raise APIError({"message": 'relation "site_visits" does not exist', "code": "42P01", "hint": None, "details": None})

    monkeypatch.setattr(visitors, "admin_client", missing)
    response = client.get("/visitors/stats", headers=HEADERS)
    assert response.status_code == 503
    assert "isn't set up" in response.json()["detail"]


def test_visitors_agent_reads_the_numbers_and_reports(client, claude, visits, store):
    claude["replies"] += [
        message(tool_use("visitor_stats", {"days": 7}, "t1"), tool_use("compare_periods", {"days": 7}, "t2")),
        message(tool_use("story_titles", {"paths": ["/stories/free-story", "/stories/gone"]}, "t3")),
        final("2 visitors stayed 1 min 22 s on average."),
    ]
    response = client.post("/agents/visitors", json={"days": 7, "question": "How long do people stay?"}, headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["report"] == "2 visitors stayed 1 min 22 s on average."
    assert [s["tool"] for s in body["steps"]] == ["visitor_stats", "compare_periods", "story_titles"]

    # The owner's question reaches Claude fenced off, and the tools return real numbers.
    assert "<<<question\nHow long do people stay?\nquestion>>>" in claude["requests"][0]["messages"][-1]["content"]
    stats, comparison = (result_text(b) for b in tool_results(claude["requests"][1]))
    assert '"visitors": 2' in stats and '"live_now": 1' in stats
    assert '"current"' in comparison
    (titles,) = (result_text(b) for b in tool_results(claude["requests"][2]))
    assert titles == '{"/stories/free-story": "A Free Story", "/stories/gone": null}'

    (run,) = store["agent_runs"]
    assert run["agent"] == "visitors" and run["status"] == "done"
    assert run["detail"] == {"days": 7, "question": True}


def test_visitors_agent_rejects_a_bad_period(client, claude, visits):
    claude["replies"] += [message(tool_use("visitor_stats", {"days": 400}, "t1")), final("Done.")]
    response = client.post("/agents/visitors", json={}, headers=HEADERS)
    assert response.status_code == 200, response.text
    (error,) = tool_results(claude["requests"][1])
    assert error.get("is_error") and "between 1 and 90" in result_text(error)
    assert client.post("/agents/visitors", json={"days": 0}, headers=HEADERS).status_code == 422
