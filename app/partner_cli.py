"""Issue, revoke and list partner app keys. Run from the API folder:

    .venv/bin/python -m app.partner_cli issue "Acme Bedtime Bot" --daily-limit 5
    .venv/bin/python -m app.partner_cli revoke "Acme Bedtime Bot"
    .venv/bin/python -m app.partner_cli list

Uses SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from .env, so it writes to the
same database the deployed API reads. A key is printed once, when it's issued:
only its hash is stored, so a lost key can't be recovered, only replaced
(revoke, then issue again under a new name).
"""

import argparse
import sys

from .partners import hash_key, new_key
from .supabase_client import admin_client


def issue(name: str, daily_limit: int) -> None:
    key = new_key()
    admin_client().table("partner_apps").insert(
        {"name": name, "key_hash": hash_key(key), "daily_limit": daily_limit}
    ).execute()
    print(f"Issued a key for {name!r} ({daily_limit} stories per day). It won't be shown again:\n\n{key}\n")


def revoke(name: str) -> None:
    rows = admin_client().table("partner_apps").update({"active": False}).eq("name", name).execute().data
    if not rows:
        sys.exit(f"No partner app named {name!r}.")
    print(f"Revoked {name!r}. Its key stops working immediately.")


def list_apps() -> None:
    rows = (
        admin_client()
        .table("partner_apps")
        .select("name, active, daily_limit, runs_today, run_day, last_used_at")
        .order("created_at")
        .execute()
        .data
        or []
    )
    if not rows:
        print("No partner apps yet.")
    for row in rows:
        state = "active " if row["active"] else "revoked"
        print(
            f"{state}  {row['name']}  limit {row['daily_limit']}/day, "
            f"{row['runs_today']} on {row['run_day'] or '-'}, last used {row['last_used_at'] or 'never'}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.partner_cli", description="Manage partner app keys.")
    commands = parser.add_subparsers(dest="command", required=True)
    issue_cmd = commands.add_parser("issue", help="Create a partner app and print its key.")
    issue_cmd.add_argument("name")
    issue_cmd.add_argument("--daily-limit", type=int, default=5)
    revoke_cmd = commands.add_parser("revoke", help="Turn a partner app's key off.")
    revoke_cmd.add_argument("name")
    commands.add_parser("list", help="Show every partner app and its usage.")

    args = parser.parse_args()
    if args.command == "issue":
        issue(args.name, args.daily_limit)
    elif args.command == "revoke":
        revoke(args.name)
    else:
        list_apps()


if __name__ == "__main__":
    main()
