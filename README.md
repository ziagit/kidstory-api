# kidstory-api

A FastAPI service for the [Kidstory](../kidstory) story catalog. It reads and writes the
same Supabase Postgres database the Nuxt app uses, so anything published through
`/studio` shows up here immediately, and anything created here shows up on the site.

This is a **sibling project**, not part of the Nuxt app — the site keeps serving its own
Nitro routes (`server/api/*`). This API exists to expose the catalog as a standalone
service (story CRUD for other clients, tools, or apps).

## Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | — | Liveness plus a Supabase round-trip |
| `GET` | `/categories` | — | The six fixed category slugs and labels |
| `GET` | `/stories` | — | Newest-first listing (no body text) |
| `GET` | `/stories/{slug}` | optional reader token | One story, paywall-aware |
| `POST` | `/stories` | `X-API-Key` | Create a story |
| `PATCH` | `/stories/{slug}` | `X-API-Key` | Partial update |
| `DELETE` | `/stories/{slug}` | `X-API-Key` | Delete a story |
| `GET` | `/distance` | — | Driving distance and duration between two addresses |

Interactive docs at `/docs` once running.

### Listing filters

`GET /stories` accepts `category`, `ageRange`, `tag`, `q` (case-insensitive search over
title and excerpt), `limit` (1–100, default 20) and `offset`. It returns
`{ total, limit, offset, items }`. Story bodies are **never** included in the listing —
fetching them would leak paywalled text to anyone browsing.

```bash
curl "http://localhost:8000/stories?category=bedtime&ageRange=3-5&limit=5"
```

### Reading a story (paywall)

Mirrors the site's rule exactly: the five oldest stories are free, everything else is
`isPremium: true`. A premium story returns only its opening paragraph plus
`locked: true` and `lockedBodyCount` — the rest is withheld from the response itself,
not just hidden in a UI. To unlock, send the reader's Supabase Auth access token:

```bash
curl -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  "http://localhost:8000/stories/some-premium-story"
```

The token is verified against Supabase Auth and the user must have a row in
`entitlements` (written only by the site's Stripe webhook). An invalid or expired token
is treated as signed-out, never as an error.

### Writing

Every write needs the `X-API-Key` header matching `KIDSTORY_API_KEY`. Reads stay open,
since the site is public.

```bash
curl -X POST http://localhost:8000/stories \
  -H "X-API-Key: $KIDSTORY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "The Brave Little Kite",
    "excerpt": "A kite learns that falling is part of flying.",
    "body": ["First paragraph.", "Second paragraph."],
    "category": "adventure",
    "ageRange": "6-8",
    "emoji": "🪁",
    "tags": ["courage", "sky"]
  }'
```

Omitted fields are derived: the **slug** from the title (same `slugify()` rule as the
Nuxt app, with `-2`, `-3` … appended on collision), the **cover image** from the same
deterministic Pollinations.ai URL formula (`app/cover_image.py` is a port of
`server/utils/cover-image.ts`), and `publishedAt` from the clock. New stories default to
`isPremium: true`, matching the site's paywall rule. Pass `slug` explicitly and a
collision returns `409` instead of auto-suffixing.

Unlike the Nuxt publish route, an unknown `category` or `ageRange` is rejected with
`422` rather than silently falling back to `adventure` / `all-ages` — silent coercion is
reasonable for a model's output, but hides caller bugs in a public API.

Creating a story here writes to the database only. It does **not** trigger the owner
email or the Facebook / Instagram / YouTube auto-posts — those live in the Nuxt app's
publish pipeline and still run from `/studio` and the daily cron.

## Distance service

A self-contained utility endpoint, unrelated to the story catalog — it exists to
exercise the API end to end without touching story data.

```bash
curl -G http://localhost:8000/distance \
  --data-urlencode "origin=10 Downing Street, London" \
  --data-urlencode "destination=Buckingham Palace, London"
```

```json
{
  "origin":      { "query": "...", "displayName": "10 Downing Street, ...", "latitude": 51.5034878, "longitude": -0.1276965 },
  "destination": { "query": "...", "displayName": "Buckingham Palace, ...",  "latitude": 51.5008349, "longitude": -0.1430045 },
  "distanceMeters": 1261.7, "distanceKm": 1.26, "distanceMiles": 0.78,
  "durationSeconds": 228.9, "durationMinutes": 3.8, "durationText": "4 min",
  "straightLineKm": 1.1, "profile": "driving"
}
```

Addresses are geocoded with [Nominatim](https://nominatim.org) and routed with the
public [OSRM](https://project-osrm.org) demo server — both free, no signup, no API key,
so this works the moment the app runs. `straightLineKm` is the great-circle distance,
returned alongside the road route for context.

**Status codes:** `404` if an address can't be geocoded or there's no drivable route
between the two points; `502` if an upstream service misbehaves; `504` on timeout;
`422` for a missing or blank address.

**Courtesy limits.** Both services are volunteer-hosted for light use. This client sends
a descriptive `User-Agent`, throttles Nominatim to the 1 request/second its policy asks
for, and caches geocode results in memory (256 entries, case-insensitive), so repeat
lookups of the same address don't re-hit it. For real traffic, self-host both and point
`NOMINATIM_URL` / `OSRM_URL` at your own instances.

Only the `driving` profile is supported — the OSRM demo server only has the car profile
loaded, so offering walking or cycling would silently return driving numbers.

## Setup

```bash
cd kidstory-api
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # then fill it in
```

`.env` needs the same `SUPABASE_URL`, `SUPABASE_ANON_KEY` and
`SUPABASE_SERVICE_ROLE_KEY` as the Nuxt app's `.env`, plus a `KIDSTORY_API_KEY` of your
own (`openssl rand -hex 32`). The anon key is used for reads (RLS-guarded); the
service-role key is used only for writes and entitlement lookups, exactly as the Nuxt
app splits them. `CORS_ORIGINS` is a comma-separated allowlist of browser origins. The distance service
needs no credentials; `NOMINATIM_URL`, `OSRM_URL`, `GEO_USER_AGENT` and
`GEO_TIMEOUT_SECONDS` are optional overrides with working defaults.

## Running

```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
```

## Tests

```bash
.venv/bin/python -m pytest
```

The suite runs against an in-memory fake of the Supabase client (`tests/fakes.py`), so it
needs no credentials and touches no network.

## Layout

```
app/
  main.py             FastAPI app, CORS, router wiring
  config.py           Env-backed settings
  supabase_client.py  Anon (read) and service-role (write) clients
  security.py         X-API-Key guard, Supabase JWT, entitlement check
  schemas.py          Pydantic models; camelCase responses matching the Nuxt app
  cover_image.py      Port of the site's Pollinations cover-URL formula
  geo.py              Geocoding, routing, haversine, duration formatting
  http_client.py      Shared outbound httpx client (lifespan-managed)
  routers/            health, categories, stories, distance
tests/                pytest suite + in-memory Supabase fake
```
