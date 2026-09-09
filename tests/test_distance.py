import httpx
import pytest
from fastapi.testclient import TestClient

from app import geo
from app.geo import Place, haversine_km, humanize_duration
from app.http_client import get_http_client
from app.main import app

# Coordinates for the two addresses the mock geocoder knows about.
PLACES = {
    "10 downing street, london": ("10 Downing Street, London SW1A 2AA, United Kingdom", 51.5034, -0.1276),
    "buckingham palace, london": ("Buckingham Palace, London SW1A 1AA, United Kingdom", 51.5014, -0.1419),
}


class Upstream:
    """Records what was called, so tests can assert on caching and call counts."""

    def __init__(self, *, route_code: str = "Ok", geocode_status: int = 200) -> None:
        self.geocode_calls: list[str] = []
        self.route_calls: list[str] = []
        self.route_code = route_code
        self.geocode_status = geocode_status

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            query = request.url.params["q"]
            self.geocode_calls.append(query)
            if self.geocode_status != 200:
                return httpx.Response(self.geocode_status, json={"error": "upstream is unhappy"})
            match = PLACES.get(query.strip().casefold())
            if not match:
                return httpx.Response(200, json=[])
            display_name, lat, lon = match
            return httpx.Response(200, json=[{"display_name": display_name, "lat": str(lat), "lon": str(lon)}])

        if "/route/v1/driving/" in request.url.path:
            self.route_calls.append(request.url.path)
            if self.route_code != "Ok":
                # The real OSRM returns its own error codes with HTTP 400, not 200.
                return httpx.Response(400, json={"code": self.route_code, "message": "no route found"})
            # ~1.4 km, ~5 minutes — a plausible Downing St → Buckingham Palace drive.
            return httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 1408.6, "duration": 302.4}]})

        raise AssertionError(f"unexpected upstream request: {request.url}")


@pytest.fixture
def upstream(monkeypatch):
    # Nominatim's 1 req/sec courtesy throttle would add a real second per call.
    monkeypatch.setattr(geo, "_NOMINATIM_MIN_INTERVAL", 0.0)
    geo.clear_geocode_cache()
    yield Upstream()
    geo.clear_geocode_cache()


@pytest.fixture
def geo_client(upstream):
    def build(fake: Upstream = upstream) -> TestClient:
        mock = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
        app.dependency_overrides[get_http_client] = lambda: mock
        return TestClient(app)

    yield build
    app.dependency_overrides.pop(get_http_client, None)


def test_distance_between_two_addresses(geo_client, upstream):
    with geo_client() as client:
        response = client.get(
            "/distance",
            params={"origin": "10 Downing Street, London", "destination": "Buckingham Palace, London"},
        )
    assert response.status_code == 200
    payload = response.json()

    assert payload["origin"]["displayName"].startswith("10 Downing Street")
    assert payload["origin"]["latitude"] == 51.5034
    assert payload["destination"]["displayName"].startswith("Buckingham Palace")

    assert payload["distanceMeters"] == 1408.6
    assert payload["distanceKm"] == 1.41
    assert payload["distanceMiles"] == 0.88
    assert payload["durationSeconds"] == 302.4
    assert payload["durationMinutes"] == 5.0
    assert payload["durationText"] == "5 min"
    assert payload["profile"] == "driving"
    # Straight line must be shorter than the road route.
    assert 0 < payload["straightLineKm"] < payload["distanceKm"]

    assert len(upstream.geocode_calls) == 2
    assert len(upstream.route_calls) == 1


def test_coordinates_are_sent_to_osrm_as_lon_lat(geo_client, upstream):
    with geo_client() as client:
        client.get("/distance", params={"origin": "10 Downing Street, London", "destination": "Buckingham Palace, London"})
    # OSRM takes lon,lat — reversing this is the classic bug, so pin the order.
    assert upstream.route_calls[0].endswith("/-0.1276,51.5034;-0.1419,51.5014")


def test_repeated_address_is_served_from_the_geocode_cache(geo_client, upstream):
    params = {"origin": "10 Downing Street, London", "destination": "Buckingham Palace, London"}
    with geo_client() as client:
        client.get("/distance", params=params)
        client.get("/distance", params=params)
    assert len(upstream.geocode_calls) == 2  # not 4
    assert len(upstream.route_calls) == 2


def test_cache_is_case_insensitive(geo_client, upstream):
    with geo_client() as client:
        client.get("/distance", params={"origin": "10 Downing Street, London", "destination": "Buckingham Palace, London"})
        client.get("/distance", params={"origin": "10 DOWNING STREET, LONDON", "destination": "Buckingham Palace, London"})
    assert len(upstream.geocode_calls) == 2


def test_unknown_address_is_404(geo_client):
    with geo_client() as client:
        response = client.get("/distance", params={"origin": "Nowhere At All", "destination": "Buckingham Palace, London"})
    assert response.status_code == 404
    assert "Nowhere At All" in response.json()["detail"]


@pytest.mark.parametrize("code", ["NoRoute", "NoSegment"])
def test_no_drivable_route_is_404_even_though_osrm_answers_with_http_400(geo_client, upstream, code):
    upstream.route_code = code
    with geo_client(upstream) as client:
        response = client.get("/distance", params={"origin": "10 Downing Street, London", "destination": "Buckingham Palace, London"})
    assert response.status_code == 404
    assert response.json()["detail"] == "No drivable route between these addresses."


def test_other_routing_failures_are_502_without_leaking_the_upstream_url(geo_client, upstream):
    upstream.route_code = "InvalidQuery"
    with geo_client(upstream) as client:
        response = client.get("/distance", params={"origin": "10 Downing Street, London", "destination": "Buckingham Palace, London"})
    assert response.status_code == 502
    assert "router.project-osrm.org" not in response.json()["detail"]


def test_upstream_failure_is_502(geo_client, upstream):
    upstream.geocode_status = 500
    with geo_client(upstream) as client:
        response = client.get("/distance", params={"origin": "10 Downing Street, London", "destination": "Buckingham Palace, London"})
    assert response.status_code == 502


def test_upstream_timeout_is_504(geo_client, monkeypatch):
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    mock = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler))
    app.dependency_overrides[get_http_client] = lambda: mock
    with TestClient(app) as client:
        response = client.get("/distance", params={"origin": "10 Downing Street, London", "destination": "Buckingham Palace, London"})
    assert response.status_code == 504


def test_missing_parameters_are_422(geo_client):
    with geo_client() as client:
        assert client.get("/distance").status_code == 422
        assert client.get("/distance", params={"origin": "London"}).status_code == 422


def test_blank_address_is_rejected(geo_client):
    with geo_client() as client:
        response = client.get("/distance", params={"origin": "   ", "destination": "Buckingham Palace, London"})
    assert response.status_code == 422


def test_haversine_matches_a_known_distance():
    new_york = Place("ny", "New York", 40.7128, -74.0060)
    london = Place("ldn", "London", 51.5074, -0.1278)
    # Great-circle New York → London is ~5570 km.
    assert 5560 < haversine_km(new_york, london) < 5580
    assert haversine_km(new_york, new_york) == 0


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "1 min"), (30, "1 min"), (302.4, "5 min"), (3600, "1 h"), (4980, "1 h 23 min"), (36000, "10 h")],
)
def test_humanize_duration(seconds, expected):
    assert humanize_duration(seconds) == expected
