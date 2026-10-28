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
written: every story, whether from the site's `/studio` chat, the daily auto-post or an MCP
client, comes from the story agent (`POST /agents/story`), which runs on Claude
(`claude-opus-5-5`) through the Anthropic API.

## Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | — | Liveness plus a Supabase round-trip |
| `GET` | `/categories` | — | The six fixed category slugs and labels |
| `GET` | `/stories` | — | Newest-first listing (no body text) |
| `GET` | `/stories/{slug}` | optional reader token | One story, paywall-aware |
| `GET` | `/books` | — | Newest-first Book Picks digests (no body text) |
| `GET` | `/books/{slug}` | — | One digest: text, the books it names, its sources |
| `POST` | `/stories` | `X-API-Key` | Create a story |
| `POST` | `/agents/story` | `X-API-Key` | Run the story agent: write or revise a story and check it (nothing saved) |
| `POST` | `/agents/social` | `X-API-Key` | Run the social agent: write and publish posts for a published story |
| `POST` | `/agents/books` | `X-API-Key` | Run the books agent: research children's bestsellers and publish a digest |
| `POST` | `/agents/support` | `X-API-Key` | Run the support agent on one message from the site's chat bubble |
| `POST` | `/partner/stories` | partner key | For partner apps: the story agent writes a story about a topic and publishes it |
| `PATCH` | `/stories/{slug}` | `X-API-Key` | Partial update |
| `DELETE` | `/stories/{slug}` | `X-API-Key` | Delete a story |
| `POST` | `/mcp` | optional bearer | MCP endpoint (see below) |
| `POST` | `/partner/mcp` | partner key | MCP endpoint for partner apps (see below) |

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

### Agents

Four Claude agents run here, all on the SDK's tool runner (`app/agents/`). Each one decides
its own steps: it calls tools, reads what they return, and keeps going until it's done.
They use `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` (default `claude-opus-5-5`) through the
official `anthropic` SDK, at low effort (short stories don't need deep reasoning). They opt
into Anthropic's server-side refusal fallback (`fallbacks: "default"`), so a benign request
that a safety classifier wrongly declines is retried on another model; if Claude still
declines, the endpoint answers 422.

**Story agent: `POST /agents/story`.** Every story is written here: the site's `/studio`
chat, the daily auto-post and the MCP `generate_story` tool all call it. Tools:

- `catalog_overview`: story counts per category and age range, plus the newest titles.
- `search_catalog`: existing stories matching a word or phrase, to avoid near-copies.
- `submit_story`: the finished story. It's checked against the catalog's rules: 2–4
  paragraphs of 150–450 words, a valid category and age range, a title that isn't taken,
  1–4 tags, and a short excerpt. Problems go back to the agent to fix, so it revises
  instead of failing the run.

Every field in the body is optional. With no `topic` the agent chooses its own premise,
and with no `category`/`ageRange` it picks whatever the catalog has fewest of; given, they
are fixed and `submit_story` holds the agent to them.

To revise a story, send the current `draft` with the change in `topic` ("make it
shorter") and the `history` from the previous response. The agent edits that draft
instead of starting over, and the response's `history` includes the new turn. This is how
the `/studio` chat refines a story.

```bash
curl -X POST http://localhost:8000/agents/story \
  -H "X-API-Key: $KIDSTORY_API_KEY" -H "Content-Type: application/json" \
  -d '{ "topic": "an owl who cannot fall asleep" }'
```

Response: `{ "draft": { title, emoji, excerpt, category, ageRange, readTimeMinutes, tags,
body }, "notes": "why it chose this story (or what it changed)", "steps": [{ "tool",
"detail" }, ...], "history": [...] }`. Nothing is saved: the draft is exactly what
`POST /stories` accepts, so publishing is a second call with the same body.

**Social agent: `POST /agents/social`.** It runs for a story that's already published
(`{ "slug": "...", "dryRun": false }`). It reads the story, checks the last few posts on
each account (`recent_posts`), then writes and publishes a post for each platform
(`post_to_facebook`, `post_to_instagram`). The link and the image are fixed by the server,
so the agent controls only the words. The post tools enforce each platform's limits before
anything is sent, and an Instagram caption without the story URL is sent back to the agent.
Each platform gets at most one post per run and at most two attempts.

A platform with blank credentials (`FACEBOOK_*`, `INSTAGRAM_*`) is left out, and with none
configured no model call is made. `dryRun: true` writes the posts without publishing them.
Response: `{ "posts": [{ platform, status: posted|dry_run|skipped|failed, text, postId,
error }], "notes", "steps" }`. A rejected post shows up as `failed`; it doesn't fail the
request. YouTube isn't part of the agent: its upload needs ffmpeg, so it stays in the
Nuxt app.

**Support agent: `POST /agents/support`.** It answers readers in the site's chat bubble,
one message per call: how sign-in, the free stories and Full Access work, and which stories
suit a child (`find_stories`, `story_details`). Its tools only read the catalog. When a
reader needs something only the owner can do (a refund, a payment that didn't unlock, a
bug), it calls `hand_off_to_owner`, and the response's `handoff` says what to pass on; the
Nuxt app emails it to the owner. The reader's message is fenced off in the prompt as their
own text, never as instructions. Forward the reader's Supabase token as
`Authorization: Bearer <jwt>` so the agent knows whether they're signed in and have Full
Access. Body: `{ "message", "history"? }`. Response: `{ "reply", "handoff": { summary,
contactEmail } | null, "steps", "history" }`; send `history` back with the next message.

**Books agent: `POST /agents/books`.** It writes the daily Book Picks digest on the
children's books selling best. It looks at recent digests so today's takes a different
angle (an age band, a genre, a new release), searches the web with Anthropic's server-side
web search (`web_search_20250305`, up to 8 searches), and submits a digest with
`publish_digest`: 3–5 paragraphs (180–500 words), 3–8 books with author, ages and a
one-line why, and at least two sources. Every source must be a page its searches returned
this run, so it can't cite a made-up list. The digest is saved to the `book_digests`
table (created by the Nuxt repo's `supabase/migrations/004_book_digests.sql`; until then
the endpoint and `GET /books` answer 503) and read back through `GET /books`. Body:
`{ "focus"?, "dryRun"? }`. Response: `{ "digest", "published", "notes", "steps" }`. Share
a digest with `POST /agents/social` and `{ "slug", "kind": "books" }`. Its Claude calls
get a 150s timeout since they run searches; a run takes about a minute.

The basic search tool is deliberate: `web_search_20260209` runs its searches from code in a
sandbox, and when that code crashed it repeated the same search until `max_uses` ran out.

### Partner apps

Other people's apps and agents can create stories with keys of their own. They send a
topic only: the story agent writes every word, treats the topic as a suggestion rather
than instructions (it's fenced off in the prompt), may decline it with a
`decline_request` tool that only partner runs get, and the story is published as soon
as it passes `submit_story`. It's tagged with the app's name in `stories.created_by`. Hand
partners [PARTNERS.md](PARTNERS.md): it's written for them.

- **REST:** `POST /partner/stories` with `Authorization: Bearer <partner key>` and
  `{ "topic", "category"?, "ageRange"? }`. It returns `201` with
  `{ story, url, notes }`.
- **MCP:** `/partner/mcp`, a separate server gated on a partner key. It has the three
  read tools plus `create_story`, which forwards the caller's key to the REST route. It
  has no edit, delete or raw-create tools, whatever `MCP_PUBLIC_WRITES` says.
- **Limits:** `partner_apps.daily_limit` runs per UTC day, counted when a run starts, so
  declined and failed runs count too. Over the limit is a `429`.

Setup, once: run the site repo's `supabase/migrations/003_partner_apps.sql` in the Supabase
SQL Editor. It adds the `partner_apps` table (RLS on, no policies, service role only) and
`stories.created_by`. Then manage keys from this folder:

```bash
.venv/bin/python -m app.partner_cli issue "Acme Bedtime Bot" --daily-limit 5
.venv/bin/python -m app.partner_cli list
.venv/bin/python -m app.partner_cli revoke "Acme Bedtime Bot"
```

`issue` prints the key once; only its SHA-256 hash is stored. Partner stories don't go
through the Nuxt publish pipeline, so there's no new-story email and no social or
YouTube post for them.

### MCP

The same catalog is exposed to MCP clients (ChatGPT / Claude connectors, Claude Code) at
`/mcp` (streamable HTTP). The tools call this API's own endpoints in-process, so the
routers stay the single implementation.

- **`MCP_BEARER_TOKEN` unset:** `/mcp` is open but **read-only** — only `list_categories`,
  `list_stories` and `get_story` are registered.
- **`MCP_BEARER_TOKEN` set:** every request to `/mcp` must carry
  `Authorization: Bearer <token>`, and the `generate_story` (runs the story agent),
  `create_story`, `update_story` and `delete_story` tools are registered too. Writes use the server's own
  `KIDSTORY_API_KEY`, which is why they only exist behind the token.

- **`MCP_PUBLIC_WRITES=1`:** registers the write and generate tools on the open endpoint
  with no auth at all. This is what lets ChatGPT's connector (which only supports "no
  auth" or OAuth) publish stories — and it also means **anyone who knows the URL can
  create, edit or delete stories** and spend the model budget. Off by default.

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

For story generation set `ANTHROPIC_API_KEY` (from the Claude Console). See
`.env.example` for `MCP_BEARER_TOKEN` and `PUBLIC_HOSTS`.

## Running

```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
```

## Deploying

The `Dockerfile` runs anywhere that sets `$PORT` (Render, Cloud Run, Railway, Fly, HF
Spaces).

### API on Vercel (Hobby, free, no card)

Vercel's Python runtime detects the FastAPI app in `app/main.py` with zero config
(`vercel.json` only raises `maxDuration` and trims the bundle). In the Vercel dashboard:
**Add New → Project → Import `ziagit/kidstory-api`**, add the environment variables from
`.env.example` (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`,
`KIDSTORY_API_KEY`, `ANTHROPIC_API_KEY`, `CORS_ORIGINS`, `PUBLIC_HOSTS=<project>.vercel.app`),
and deploy.
Every push to `master` redeploys.

Serverless means no process outlives a burst of requests, so `/mcp` runs **stateless**
there (`MCP_STATELESS`, on automatically when Vercel's `VERCEL` env var is present):
one JSON response per request, no session ids. MCP clients handle that transparently.

Then point the Nuxt app at it: `KIDSTORY_API_URL=https://<project>.vercel.app` and the
matching `KIDSTORY_API_KEY` in the site's Vercel env vars (and the GitHub Actions
secrets for the manual fallback workflow).

Render's free tier and HF Docker Spaces both require a card or a subscription now; the
`Dockerfile` still works on either, or on Cloud Run / Railway / Fly, if that changes.

## Tests

```bash
.venv/bin/python -m pytest
```

The suite runs against an in-memory fake of the Supabase client (`tests/fakes.py`), so it
needs no credentials and touches no network. Claude is faked the same way.

## Layout

```
app/
  main.py             App factory: CORS, routers, MCP mount + bearer gate
  config.py           Env-backed settings
  supabase_client.py  Anon (read) and service-role (write) clients
  security.py         X-API-Key guard, Supabase JWT, entitlement check
  schemas.py          Pydantic models; camelCase responses matching the Nuxt app
  agents/             The story, books, social and support agents: Claude client + tool runner (base.py) and their tools
  social.py           Facebook / Instagram Graph API calls used by the social agent
  partners.py         Partner app keys: lookup by hash, the auth dependency, daily limit
  partner_cli.py      `python -m app.partner_cli`: issue, list and revoke partner keys
  mcp_server.py       MCP tools wrapping the REST endpoints in-process
  cover_image.py      Port of the site's Pollinations cover-URL formula
  routers/            health, categories, stories, agents, partner
tests/                pytest suite + in-memory Supabase fake + fake Claude
```
