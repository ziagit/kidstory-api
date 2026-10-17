"""Facebook Page and Instagram posting through the Graph API.

Ported from the Nuxt app's shared/utils/facebook.ts and instagram.ts when the
social agent took over posting: the agent writes the text, these functions
publish it. Errors raise SocialError with the platform's own message, so the
agent can read why a post was rejected and fix it.
"""

import asyncio

import httpx

GRAPH_API = "https://graph.facebook.com/v21.0"
TIMEOUT_SECONDS = 20.0

# Instagram creates a media container asynchronously before it can be
# published. A single image is almost always ready at once; poll briefly anyway.
CONTAINER_POLL_ATTEMPTS = 6
CONTAINER_POLL_DELAY_SECONDS = 2.0


class SocialError(Exception):
    pass


def _http() -> httpx.AsyncClient:
    # Binding to 0.0.0.0 forces IPv4: graph.facebook.com resolves to IPv6 first on
    # the owner's machine, where IPv6 isn't routable (the same fix the Nuxt app
    # made with dns.setDefaultResultOrder('ipv4first')).
    return httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    )


async def _graph(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    try:
        response = await client.request(method, f"{GRAPH_API}/{path}", **kwargs)
    except httpx.HTTPError as exc:
        raise SocialError(f"Could not reach the Graph API: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.is_error:
        message = (payload.get("error") or {}).get("message") or f"HTTP {response.status_code}"
        raise SocialError(message)
    return payload


async def facebook_post(page_id: str, token: str, *, message: str, link: str) -> str:
    """Posts to the Page feed; Facebook unfurls `link` into a preview card. Returns the post id."""
    async with _http() as client:
        payload = await _graph(
            client, "POST", f"{page_id}/feed", json={"message": message, "link": link, "access_token": token}
        )
    return str(payload.get("id", ""))


async def facebook_recent(page_id: str, token: str, *, limit: int = 5) -> list[str]:
    async with _http() as client:
        payload = await _graph(
            client, "GET", f"{page_id}/posts", params={"fields": "message", "limit": limit, "access_token": token}
        )
    return [item["message"] for item in payload.get("data", []) if item.get("message")]


async def instagram_post(account_id: str, token: str, *, image_url: str, caption: str) -> str:
    """Single-image feed post: create a container, wait for it, publish it. Returns the media id."""
    async with _http() as client:
        container = await _graph(
            client,
            "POST",
            f"{account_id}/media",
            json={"image_url": image_url, "caption": caption, "access_token": token},
        )
        container_id = container.get("id")
        if not container_id:
            raise SocialError("Instagram did not return a media container id.")

        for _ in range(CONTAINER_POLL_ATTEMPTS):
            status = await _graph(
                client, "GET", container_id, params={"fields": "status_code", "access_token": token}
            )
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise SocialError(f"Instagram media container {code}.")
            await asyncio.sleep(CONTAINER_POLL_DELAY_SECONDS)
        else:
            raise SocialError("Instagram media container never finished processing.")

        published = await _graph(
            client, "POST", f"{account_id}/media_publish", json={"creation_id": container_id, "access_token": token}
        )
    return str(published.get("id", ""))


async def instagram_recent(account_id: str, token: str, *, limit: int = 5) -> list[str]:
    async with _http() as client:
        payload = await _graph(
            client, "GET", f"{account_id}/media", params={"fields": "caption", "limit": limit, "access_token": token}
        )
    return [item["caption"] for item in payload.get("data", []) if item.get("caption")]
