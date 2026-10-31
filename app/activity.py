"""Agent activity: a row in `agent_runs` per agent run and per story publish.

The admin dashboard's agent machine polls these (GET /agents/activity) and only
animates the agents and wires a real run is using. Recording is best effort: if
the table is missing or Supabase is down, the run goes ahead and a warning is logged.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .supabase_client import admin_client

log = logging.getLogger(__name__)

TABLE = "agent_runs"


@dataclass
class RunLog:
    agent: str
    action: str
    # What started the run and where its work went; filled in as the run goes.
    detail: dict[str, Any] = field(default_factory=dict)
    id: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert(row: dict[str, Any]) -> bool:
    try:
        admin_client().table(TABLE).insert(row).execute()
    except Exception as exc:  # noqa: BLE001 — activity must never break a run
        log.warning("Could not record agent activity: %s", exc)
        return False
    return True


def _finish(run: RunLog, status: str) -> None:
    if not run.id:
        return
    try:
        admin_client().table(TABLE).update(
            {"status": status, "finished_at": _now(), "detail": run.detail}
        ).eq("id", run.id).execute()
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not record agent activity: %s", exc)


@contextmanager
def tracked(agent: str, action: str = "run", **detail: Any) -> Iterator[RunLog]:
    """Records a run as 'running' now, then 'done' or 'failed' when the block ends."""
    run = RunLog(agent=agent, action=action, detail={k: v for k, v in detail.items() if v is not None})
    run_id = str(uuid4())
    row = {"id": run_id, "agent": agent, "action": action, "status": "running", "detail": run.detail, "started_at": _now()}
    if _insert(row):
        run.id = run_id
    try:
        yield run
    except BaseException:
        _finish(run, "failed")
        raise
    _finish(run, "done")


def record(agent: str, action: str, **detail: Any) -> None:
    """Records something that happened at once (a story publish) as a finished run."""
    now = _now()
    _insert(
        {
            "id": str(uuid4()),
            "agent": agent,
            "action": action,
            "status": "done",
            "detail": {k: v for k, v in detail.items() if v is not None},
            "started_at": now,
            "finished_at": now,
        }
    )


def recent(seconds: int) -> list[dict[str, Any]]:
    """Runs started in the last `seconds`, newest first."""
    since = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    rows = (
        admin_client()
        .table(TABLE)
        .select("*")
        .gte("started_at", since)
        .order("started_at", desc=True)
        .limit(100)
        .execute()
        .data
    )
    return rows or []
