"""Address → coordinates → driving route, via free no-signup OpenStreetMap services.

Geocoding uses Nominatim and routing uses the public OSRM demo server. Neither
needs an API key, which is the point: this service works as soon as the app runs.
Both are courtesy-hosted, so this module caches geocode results and throttles
Nominatim to the 1 request/second its usage policy asks for.
"""

import asyncio
import math
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx

from .config import get_settings

# Nominatim's usage policy: max 1 request/second, and a real User-Agent.
_NOMINATIM_MIN_INTERVAL = 1.0
_nominatim_lock = asyncio.Lock()
_nominatim_last_call = 0.0

# OSRM codes that mean "these points are fine, there is just no road between them".
_NO_ROUTE_CODES = {"NoRoute", "NoSegment"}

_GEOCODE_CACHE_MAX = 256
_geocode_cache: "OrderedDict[str, Place]" = OrderedDict()


@dataclass(frozen=True)
class Place:
    query: str
    display_name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Route:
    distance_meters: float
    duration_seconds: float


class GeoError(Exception):
    """An upstream failure worth surfacing to the caller with a specific status."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _cache_get(key: str) -> Place | None:
    place = _geocode_cache.get(key)
    if place is not None:
        _geocode_cache.move_to_end(key)
    return place


def _cache_put(key: str, place: Place) -> None:
    _geocode_cache[key] = place
    _geocode_cache.move_to_end(key)
    while len(_geocode_cache) > _GEOCODE_CACHE_MAX:
        _geocode_cache.popitem(last=False)


def clear_geocode_cache() -> None:
    _geocode_cache.clear()


async def _throttle_nominatim() -> None:
    global _nominatim_last_call
    async with _nominatim_lock:
        elapsed = time.monotonic() - _nominatim_last_call
        if elapsed < _NOMINATIM_MIN_INTERVAL:
            await asyncio.sleep(_NOMINATIM_MIN_INTERVAL - elapsed)
        _nominatim_last_call = time.monotonic()


async def geocode(client: httpx.AsyncClient, address: str) -> Place:
    """Resolves a free-text address to a single best-match coordinate."""
    query = address.strip()
    if not query:
        raise GeoError(422, "Address must not be empty.")

    cache_key = query.casefold()
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    settings = get_settings()
    await _throttle_nominatim()
    try:
        response = await client.get(
            f"{settings.nominatim_url}/search",
            params={"q": query, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": settings.geo_user_agent},
            timeout=settings.geo_timeout_seconds,
        )
        response.raise_for_status()
        results = response.json()
    except httpx.TimeoutException as exc:
        raise GeoError(504, f"Geocoding timed out for '{query}'.") from exc
    except httpx.HTTPStatusError as exc:
        raise GeoError(502, f"Geocoding service returned HTTP {exc.response.status_code}.") from exc
    except httpx.HTTPError as exc:
        raise GeoError(502, f"Geocoding request failed: {type(exc).__name__}") from exc

    if not results:
        raise GeoError(404, f"No location found for '{query}'.")

    match = results[0]
    place = Place(
        query=query,
        display_name=match.get("display_name", query),
        latitude=float(match["lat"]),
        longitude=float(match["lon"]),
    )
    _cache_put(cache_key, place)
    return place


async def road_route(client: httpx.AsyncClient, origin: Place, destination: Place) -> Route:
    """Driving distance and duration along real roads, from OSRM."""
    settings = get_settings()
    coordinates = (
        f"{origin.longitude},{origin.latitude};{destination.longitude},{destination.latitude}"
    )
    try:
        response = await client.get(
            f"{settings.osrm_url}/route/v1/driving/{coordinates}",
            params={"overview": "false"},
            timeout=settings.geo_timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise GeoError(504, "Routing timed out.") from exc
    except httpx.HTTPError as exc:
        raise GeoError(502, f"Routing request failed: {type(exc).__name__}") from exc

    # OSRM reports its own failures as HTTP 400 with a `code` in the body — so the
    # body has to be read before the status is judged, or "no route across the
    # ocean" would surface as an opaque upstream 502.
    try:
        payload = response.json()
    except ValueError:
        raise GeoError(502, f"Routing service returned a non-JSON response (HTTP {response.status_code}).") from None

    code = payload.get("code")
    if code in _NO_ROUTE_CODES:
        raise GeoError(404, "No drivable route between these addresses.")
    if code != "Ok" or not payload.get("routes"):
        detail = payload.get("message") or code or f"HTTP {response.status_code}"
        raise GeoError(502, f"Routing service could not handle this request: {detail}")

    best = payload["routes"][0]
    return Route(distance_meters=float(best["distance"]), duration_seconds=float(best["duration"]))


def haversine_km(origin: Place, destination: Place) -> float:
    """Great-circle distance — reported alongside the road route for context."""
    earth_radius_km = 6371.0088
    lat1, lon1, lat2, lon2 = map(
        math.radians, (origin.latitude, origin.longitude, destination.latitude, destination.longitude)
    )
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 2 * earth_radius_km * math.asin(math.sqrt(a))


def humanize_duration(seconds: float) -> str:
    total_minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    if hours:
        return f"{hours} h"
    return f"{minutes} min"
