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

    # Story model server — an OpenAI-compatible endpoint (the llama.cpp Space in
    # deploy/model-space/ serving ziaflutter/tinyllama-cat-mouse-story), called
    # through huggingface_hub's InferenceClient. Blank disables /stories/generate.
    story_model_url: str = ""
    story_model_api_key: str = ""
    story_model_name: str = "kidstory-story-model"
    story_model_timeout: float = 120.0

    # Bearer token required on /mcp. Blank leaves /mcp open but read-only — the
    # write and generate tools are only registered when a token protects them,
    # so a public /mcp can never spend the server's own KIDSTORY_API_KEY.
    mcp_bearer_token: str = ""

    # Comma-separated hostnames this server is publicly reached through (e.g.
    # "ziaflutter-kidstory-api.hf.space"). Enables the MCP transport's
    # DNS-rebinding protection for those hosts; blank turns the check off.
    public_hosts: str = ""

    def cors_origin_list(self) -> list[str]:
        return _split_csv(self.cors_origins)

    def public_host_list(self) -> list[str]:
        return _split_csv(self.public_hosts)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
