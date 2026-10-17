"""Partner apps: other people's agents that create stories through the story agent.

Each partner app has its own key, sent as `Authorization: Bearer <key>` on
POST /partner/stories and on every /partner/mcp request. Only the key's SHA-256
hash is stored (`partner_apps.key_hash`), so the table alone can't be used to
call the API. Keys are issued and revoked with `python -m app.partner_cli`.

A partner can only send a topic: the story agent writes every word, and the
story is published as soon as it passes the agent's checks. `daily_limit` caps
story-agent runs per UTC day, counted when a run starts, so a run the agent
declines or fails still counts.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Header, HTTPException, status
from postgrest.exceptions import APIError

from .supabase_client import admin_client

KEY_PREFIX = "ksk_"


@dataclass(frozen=True)
class PartnerApp:
    id: str
    name: str
    daily_limit: int


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        return ""
    return authorization[len("Bearer ") :].strip()


def find_partner(key: str) -> PartnerApp | None:
    """The active partner app this key belongs to, or None."""
    if not key.startswith(KEY_PREFIX):
        return None
    try:
        rows = (
            admin_client()
            .table("partner_apps")
            .select("id, name, daily_limit")
            .eq("key_hash", hash_key(key))
            .eq("active", True)
            .limit(1)
            .execute()
            .data
            or []
        )
    except APIError as exc:
        # Most likely the partner_apps migration hasn't been run on this database.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Partner access isn't set up on this server: {exc.message or exc}",
        ) from exc
    if not rows:
        return None
    row = rows[0]
    return PartnerApp(id=row["id"], name=row["name"], daily_limit=row["daily_limit"])


async def require_partner(authorization: str | None = Header(default=None)) -> PartnerApp:
    partner = find_partner(bearer_token(authorization))
    if partner is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, revoked or missing partner key (send Authorization: Bearer <key>).",
        )
    return partner


def claim_run(partner: PartnerApp) -> None:
    """Counts one story-agent run against today's limit, or raises 429 when it's used up.

    A read then a write, not one atomic step: two runs racing at the limit can
    both get through. That's an overshoot of one, which is acceptable for a
    daily cap on a handful of runs.
    """
    table = admin_client().table("partner_apps")
    row = (table.select("runs_today, run_day").eq("id", partner.id).limit(1).execute().data or [{}])[0]
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    used = (row.get("runs_today") or 0) if row.get("run_day") == today else 0
    if used >= partner.daily_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily limit reached ({partner.daily_limit} stories per day). It resets at 00:00 UTC.",
        )
    table.update({"runs_today": used + 1, "run_day": today, "last_used_at": now.isoformat()}).eq(
        "id", partner.id
    ).execute()
