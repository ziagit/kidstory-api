"""Site visits: what the Nuxt site's visit tracker recorded, summed up.

The tracker (the Nuxt app's `app/plugins/visit-tracker.client.ts`) keeps one
row per visit in `site_visits`: an anonymous browser id, when the visit started
and was last seen, the seconds the reader was really there, and the pages with
the time spent on each. The admin's Visitors page (GET /visitors/stats) and the
visitors agent both read these summaries; neither sees a single visit's row.
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from .supabase_client import admin_client

TABLE = "site_visits"
TOTALS_VIEW = "site_visit_totals"
COLUMNS = "visitor_id, is_new, started_at, last_seen_at, active_seconds, page_views, pages, entry_path, referrer, device, country"
# Days are counted in the owner's time zone (Pakistan has no daylight saving).
OWNER_TZ = timezone(timedelta(hours=5))
# The tracker reports every 30 s while a tab is open and in use.
LIVE_SECONDS = 120
# A visit that stays on one page for less than this didn't really start.
BOUNCE_SECONDS = 10
PAGE_SIZE = 1000
MAX_ROWS = 50_000
TOP = 10


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def visits_since(since: datetime) -> list[dict[str, Any]]:
    """Every visit that started at or after `since`, oldest first."""
    rows: list[dict[str, Any]] = []
    while len(rows) < MAX_ROWS:
        page = (
            admin_client()
            .table(TABLE)
            .select(COLUMNS)
            .gte("started_at", since.astimezone(timezone.utc).isoformat())
            .order("started_at")
            .range(len(rows), len(rows) + PAGE_SIZE - 1)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
    return rows


def all_time() -> dict[str, Any]:
    """Visitors, visits and time on the site since tracking began."""
    rows = admin_client().table(TOTALS_VIEW).select("*").limit(1).execute().data or []
    row = rows[0] if rows else {}
    visits = row.get("visits") or 0
    seconds = int(row.get("active_seconds") or 0)
    return {
        "visitors": row.get("visitors") or 0,
        "visits": visits,
        "page_views": row.get("page_views") or 0,
        "total_seconds": seconds,
        "avg_visit_seconds": round(seconds / visits) if visits else 0,
        "tracking_since": row.get("first_visit_at"),
    }


def live_now(now: datetime | None = None) -> int:
    """Visitors whose tracker reported in the last two minutes."""
    since = (now or datetime.now(timezone.utc)) - timedelta(seconds=LIVE_SECONDS)
    rows = admin_client().table(TABLE).select("visitor_id").gte("last_seen_at", since.isoformat()).execute().data or []
    return len({row["visitor_id"] for row in rows})


def summarize(rows: list[dict[str, Any]], since: datetime, until: datetime) -> dict[str, Any]:
    """Visitors, visits, time on the site and where people went, for visits started in [since, until)."""
    rows = [row for row in rows if since <= _parse(row["started_at"]) < until]
    durations = [int(row.get("active_seconds") or 0) for row in rows]
    visitors = {row["visitor_id"] for row in rows}
    new_visitors = {row["visitor_id"] for row in rows if row.get("is_new")}
    total = sum(durations)

    days: dict[str, dict[str, Any]] = {}
    day = since.astimezone(OWNER_TZ).date()
    while day <= (until - timedelta(seconds=1)).astimezone(OWNER_TZ).date():
        days[day.isoformat()] = {"visitors": set(), "visits": 0, "seconds": 0}
        day += timedelta(days=1)
    pages: dict[str, dict[str, int]] = defaultdict(lambda: {"visits": 0, "seconds": 0})
    for row in rows:
        bucket = days.setdefault(_parse(row["started_at"]).astimezone(OWNER_TZ).date().isoformat(), {"visitors": set(), "visits": 0, "seconds": 0})
        bucket["visitors"].add(row["visitor_id"])
        bucket["visits"] += 1
        bucket["seconds"] += int(row.get("active_seconds") or 0)
        for page in row.get("pages") or []:
            pages[page["path"]]["visits"] += 1
            pages[page["path"]]["seconds"] += int(page.get("seconds") or 0)

    def top(field: str) -> list[dict[str, Any]]:
        return [{"name": name, "visits": n} for name, n in Counter(row.get(field) or "unknown" for row in rows).most_common(TOP)]

    return {
        "from": since.isoformat(),
        "to": until.isoformat(),
        "visitors": len(visitors),
        "new_visitors": len(new_visitors),
        "returning_visitors": len(visitors - new_visitors),
        "visits": len(rows),
        "page_views": sum(int(row.get("page_views") or 0) for row in rows),
        "total_seconds": total,
        "avg_visit_seconds": round(total / len(rows)) if rows else 0,
        "median_visit_seconds": round(median(durations)) if durations else 0,
        "avg_seconds_per_visitor": round(total / len(visitors)) if visitors else 0,
        "bounce_rate": round(
            sum(1 for row in rows if int(row.get("page_views") or 0) <= 1 and int(row.get("active_seconds") or 0) < BOUNCE_SECONDS)
            / len(rows),
            3,
        )
        if rows
        else 0,
        "daily": [
            {
                "date": date,
                "visitors": len(b["visitors"]),
                "visits": b["visits"],
                "avg_visit_seconds": round(b["seconds"] / b["visits"]) if b["visits"] else 0,
            }
            for date, b in sorted(days.items())
        ],
        "top_pages": sorted(
            (
                {"path": path, "visits": p["visits"], "seconds": p["seconds"], "avg_seconds": round(p["seconds"] / p["visits"])}
                for path, p in pages.items()
            ),
            key=lambda p: (-p["visits"], -p["seconds"]),
        )[:TOP],
        "entry_pages": top("entry_path"),
        "referrers": top("referrer"),
        "devices": top("device"),
        "countries": top("country"),
    }


def visitor_stats(days: int, now: datetime | None = None) -> dict[str, Any]:
    """The last `days` days (today included, in the owner's time zone), plus all-time totals and who's here now."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(OWNER_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    since = today - timedelta(days=days - 1)
    return {
        "days": days,
        "live_now": live_now(now),
        "all_time": all_time(),
        "period": summarize(visits_since(since), since, now + timedelta(seconds=1)),
    }


def compare_periods(days: int, now: datetime | None = None) -> dict[str, Any]:
    """The last `days` days against the `days` before them, both ending at the same time of day."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=2 * days)
    middle = now - timedelta(days=days)
    rows = visits_since(start)
    keys = ("visitors", "new_visitors", "visits", "page_views", "avg_visit_seconds", "median_visit_seconds", "bounce_rate")
    current, previous = summarize(rows, middle, now), summarize(rows, start, middle)
    return {
        "days": days,
        "current": {k: current[k] for k in keys} | {"top_pages": current["top_pages"][:5]},
        "previous": {k: previous[k] for k in keys} | {"top_pages": previous["top_pages"][:5]},
    }
