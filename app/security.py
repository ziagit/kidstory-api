import secrets

from fastapi import Depends, Header, HTTPException, status

from .config import get_settings
from .supabase_client import admin_client, public_client


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Guards every write endpoint. Compared in constant time to avoid leaking the key.

    Reads are open (the site is public); creating, updating and deleting stories
    is owner-only, so it needs the shared secret from KIDSTORY_API_KEY.
    """
    settings = get_settings()
    if not settings.kidstory_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Write access is not configured on this server (KIDSTORY_API_KEY is unset).",
        )
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.kidstory_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key.")


async def current_user_id(authorization: str | None = Header(default=None)) -> str | None:
    """Resolves the reader's Supabase Auth id from `Authorization: Bearer <jwt>`.

    Returns None for anonymous callers and for tokens Supabase rejects — an
    unreadable token is treated as signed-out, never as an error, so browsing
    public stories with a stale session still works.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer ") :].strip()
    if not token:
        return None
    try:
        response = public_client().auth.get_user(token)
    except Exception:
        return None
    user = getattr(response, "user", None)
    return user.id if user else None


def has_full_access(user_id: str | None) -> bool:
    """Whether this user has purchased full access (a row in `entitlements`).

    Read with the service-role client: `entitlements` has RLS on with no policies,
    so the anon key can never see it.
    """
    if not user_id:
        return False
    response = admin_client().table("entitlements").select("user_id").eq("user_id", user_id).limit(1).execute()
    return bool(response.data)


async def full_access_dependency(user_id: str | None = Depends(current_user_id)) -> bool:
    return has_full_access(user_id)
