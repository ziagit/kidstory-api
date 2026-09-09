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
app splits them. `CORS_ORIGINS` is a comma-separated allowlist of browser origins.

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
  routers/            health, categories, stories
tests/                pytest suite + in-memory Supabase fake
```
