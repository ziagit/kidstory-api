from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment configuration, mirroring the Nuxt app's runtimeConfig split.

    The anon key is the public, RLS-guarded credential used for reads; the
    service-role key bypasses RLS and is only ever used for writes and for
    entitlement lookups. Both point at the same Supabase project as the Nuxt app.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""

    # Shared secret required on every write endpoint (X-API-Key header).
    kidstory_api_key: str = ""

    # Comma-separated origins allowed to call this API from a browser.
    cors_origins: str = "http://localhost:3000"

    # Distance service. Both defaults are free, keyless OpenStreetMap services;
    # point them at self-hosted instances for anything beyond light testing.
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    osrm_url: str = "https://router.project-osrm.org"
    # Nominatim's usage policy requires a descriptive, contactable User-Agent.
    geo_user_agent: str = "kidstory-api/0.1 (https://github.com/ziagit)"
    geo_timeout_seconds: float = 15.0

    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
