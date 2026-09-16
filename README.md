---
title: Kidstory API
emoji: 📚
colorFrom: pink
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
---

# kidstory-api

A FastAPI service for the [Kidstory](../kidstory) story catalog. It reads and writes the
same Supabase Postgres database the Nuxt app uses, so anything published through
`/studio` shows up here immediately, and anything created here shows up on the site.

This is a **sibling project**, not part of the Nuxt app — the site keeps serving its own
Nitro routes (`server/api/*`). This API exists to expose the catalog as a standalone
service (story CRUD for other clients, tools, or apps), and it is where new stories are
drafted: `POST /stories/generate` runs Kidstory's own fine-tuned model,
[`ziaflutter/tinyllama-cat-mouse-story`](https://huggingface.co/ziaflutter/tinyllama-cat-mouse-story).

## Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | — | Liveness plus a Supabase round-trip |
| `GET` | `/categories` | — | The six fixed category slugs and labels |
| `GET` | `/stories` | — | Newest-first listing (no body text) |
| `GET` | `/stories/{slug}` | optional reader token | One story, paywall-aware |
| `POST` | `/stories` | `X-API-Key` | Create a story |
| `POST` | `/stories/generate` | `X-API-Key` | Draft a story with the Kidstory model (nothing saved) |
| `PATCH` | `/stories/{slug}` | `X-API-Key` | Partial update |
| `DELETE` | `/stories/{slug}` | `X-API-Key` | Delete a story |
| `POST` | `/mcp` | optional bearer | MCP endpoint (see below) |

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

### Generating a story

`POST /stories/generate` drafts a story with the Kidstory fine-tune and returns it in
exactly the shape `POST /stories` accepts, so publishing is a second call with the same
body. Nothing is saved by the generate call itself. It needs the API key because every
call costs real model compute.

```bash
curl -X POST http://localhost:8000/stories/generate \
  -H "X-API-Key: $KIDSTORY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{ "topic": "a cat and a mouse who become friends", "category": "animals", "ageRange": "3-5" }'
```

Response: `{ "draft": { title, emoji, excerpt, category, ageRange, readTimeMinutes, tags, body },
"history": [...] }`. Send `history` back with a follow-up `topic` ("make it shorter") to
revise the same story — this is what the site's `/studio` chat does.

The model is a 1.1B-parameter prose model, not a JSON one: it writes story text and
nothing else. `app/story_generator.py` derives the rest — the title from the topic (or a
leading title line, if the model wrote one), the excerpt from the opening sentence,
emoji and tags from keywords in the text, read time from word count — and the request's
`category`/`ageRange` pass straight through. On a free CPU Space a story takes roughly
20–40 seconds; the `/studio` UI and the daily cron already wait that long.

The model is called through `huggingface_hub`'s `InferenceClient`, pointed at any
OpenAI-compatible server by `STORY_MODEL_URL` / `STORY_MODEL_NAME` / `STORY_MODEL_API_KEY`.
The intended server is the llama.cpp container in
[`deploy/model-space/`](deploy/model-space/README.md) — Hugging Face's shared inference
API doesn't serve custom fine-tunes, so the model needs a host of its own (a VPS). Until
that exists, the same endpoint runs against **OpenRouter** as a stand-in
(`STORY_MODEL_URL=https://openrouter.ai/api/v1`, a free model, `STORY_MODEL_MAX_TOKENS=1200`);
swapping to the fine-tune later is an env-var change, nothing in the site moves.

### MCP

The same catalog is exposed to MCP clients (ChatGPT / Claude connectors, Claude Code) at
`/mcp` (streamable HTTP). The tools call this API's own endpoints in-process, so the
routers stay the single implementation.

- **`MCP_BEARER_TOKEN` unset:** `/mcp` is open but **read-only** — only `list_categories`,
  `list_stories` and `get_story` are registered.
- **`MCP_BEARER_TOKEN` set:** every request to `/mcp` must carry
  `Authorization: Bearer <token>`, and the `generate_story`, `create_story`,
  `update_story` and `delete_story` tools are registered too. Writes use the server's own
  `KIDSTORY_API_KEY`, which is why they only exist behind the token.

Set `PUBLIC_HOSTS` to the hostname(s) the API is served under to turn on the MCP
transport's DNS-rebinding protection.

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

For story generation set `STORY_MODEL_URL` (the model Space's URL plus `/v1`) and
`STORY_MODEL_API_KEY` (the Space's `LLAMA_API_KEY`). See `.env.example` for `MCP_BEARER_TOKEN`
and `PUBLIC_HOSTS`.

## Running

```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
```

## Deploying

The `Dockerfile` runs anywhere that sets `$PORT` (Render, Cloud Run, Railway, Fly, HF
Spaces). Two containers are involved: this API, and the model server in
[`deploy/model-space/`](deploy/model-space/README.md).

### API on Vercel (Hobby, free, no card)

Vercel's Python runtime detects the FastAPI app in `app/main.py` with zero config
(`vercel.json` only raises `maxDuration` and trims the bundle). In the Vercel dashboard:
**Add New → Project → Import `ziagit/kidstory-api`**, add the environment variables from
`.env.example` (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`,
`KIDSTORY_API_KEY`, `CORS_ORIGINS`, `PUBLIC_HOSTS=<project>.vercel.app`), and deploy.
Every push to `master` redeploys.

Serverless means no process outlives a burst of requests, so `/mcp` runs **stateless**
there (`MCP_STATELESS`, on automatically when Vercel's `VERCEL` env var is present):
one JSON response per request, no session ids. MCP clients handle that transparently.

Then point the Nuxt app at it: `KIDSTORY_API_URL=https://<project>.vercel.app` and the
matching `KIDSTORY_API_KEY` in the site's Vercel env vars (and the GitHub Actions
secrets for the manual fallback workflow).

Render's free tier and HF Docker Spaces both require a card or a subscription now; the
`Dockerfile` still works on either, or on Cloud Run / Railway / Fly, if that changes.

### Model server

The model needs ~2 GB of RAM and a real CPU — nothing serverless-and-free runs it, and
Hugging Face now requires a PRO subscription for Docker Spaces — so `deploy/model-space/`
is written as a Space but is host-agnostic. Options:
Google Cloud Run or Modal (both $0 at this traffic with a card on file), an Oracle
Always Free VM, or your own machine behind a Cloudflare Tunnel. Whichever you pick,
set `STORY_MODEL_URL=https://<model-host>/v1` and `STORY_MODEL_API_KEY` on the API;
until then `POST /stories/generate` answers 503 and everything else works.

## Tests

```bash
.venv/bin/python -m pytest
```

The suite runs against an in-memory fake of the Supabase client (`tests/fakes.py`), so it
needs no credentials and touches no network.

## Layout

```
app/
  main.py             App factory: CORS, routers, MCP mount + bearer gate
  config.py           Env-backed settings
  supabase_client.py  Anon (read) and service-role (write) clients
  security.py         X-API-Key guard, Supabase JWT, entitlement check
  schemas.py          Pydantic models; camelCase responses matching the Nuxt app
  story_generator.py  Calls the story model (huggingface_hub) and derives draft metadata
  mcp_server.py       MCP tools wrapping the REST endpoints in-process
  cover_image.py      Port of the site's Pollinations cover-URL formula
  routers/            health, categories, stories
deploy/model-space/   The llama.cpp Space that serves the fine-tuned model
tests/                pytest suite + in-memory Supabase fake + fake model
```
