from functools import lru_cache

from fastapi import HTTPException
from supabase import Client, create_client

from .config import get_settings


@lru_cache
def public_client() -> Client:
    """Anon-key client: read-only, subject to the `stories` RLS read policy."""
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise HTTPException(status_code=500, detail="Supabase is not configured on the server.")
    return create_client(settings.supabase_url, settings.supabase_anon_key)


@lru_cache
def admin_client() -> Client:
    """Service-role client: bypasses RLS. Writes only."""
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise HTTPException(status_code=500, detail="Supabase admin credentials are not configured on the server.")
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
