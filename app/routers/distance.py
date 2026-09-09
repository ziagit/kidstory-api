import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from ..geo import GeoError, geocode, haversine_km, humanize_duration, road_route
from ..http_client import get_http_client
from ..schemas import DistanceResult, Place

router = APIRouter(tags=["distance"])


@router.get("/distance", response_model=DistanceResult)
async def distance_between(
    origin: str = Query(min_length=1, description="Free-text start address."),
    destination: str = Query(min_length=1, description="Free-text end address."),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> DistanceResult:
    """Driving distance and duration between two addresses.

    Each address is geocoded with Nominatim, then routed with OSRM. The
    straight-line distance is returned alongside the road route for context.
    """
    try:
        # Sequential, not gathered: Nominatim's 1 req/sec throttle serializes these
        # anyway, and firing both at once only risks tripping its rate limiter.
        origin_place = await geocode(client, origin)
        destination_place = await geocode(client, destination)
        route = await road_route(client, origin_place, destination_place)
    except GeoError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Upstream geo service timed out.") from exc

    return DistanceResult(
        origin=Place(**vars(origin_place)),
        destination=Place(**vars(destination_place)),
        distance_meters=round(route.distance_meters, 1),
        distance_km=round(route.distance_meters / 1000, 2),
        distance_miles=round(route.distance_meters / 1609.344, 2),
        duration_seconds=round(route.duration_seconds, 1),
        duration_minutes=round(route.duration_seconds / 60, 1),
        duration_text=humanize_duration(route.duration_seconds),
        straight_line_km=round(haversine_km(origin_place, destination_place), 2),
    )
