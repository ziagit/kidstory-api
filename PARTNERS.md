# Creating Kidstory stories from your app

Kidstory publishes short illustrated stories for children aged 3 to 12. If you run an app
or an agent, you can have Kidstory's story agent write a story for you and publish it on
the site.

You send a **topic**. Kidstory's story agent does the rest: it checks the catalog so it
doesn't repeat an existing story, writes the title, text, excerpt, tags and emoji, and
checks the result against the catalog's rules (2–4 paragraphs, 150–450 words, a gentle
problem and a happy ending, nothing scary). The story goes live as soon as it passes.
You can't send your own story text; every word is written by Kidstory's agent.

Base URL: `https://kidstory-api-orcin.vercel.app`

## Your key

Ask the Kidstory owner for a partner key. It looks like `ksk_...`, and it's shown to them
once, when it's issued. Send it on every request:

```
Authorization: Bearer ksk_...
```

Keep it on your server, never in a browser or mobile app. Each key has a daily limit
(5 stories a day unless agreed otherwise), counted per request and reset at 00:00 UTC. A
request the agent declines still counts. If a key leaks, ask for it to be revoked; it
stops working immediately.

## Option 1: REST

```bash
curl -X POST https://kidstory-api-orcin.vercel.app/partner/stories \
  -H "Authorization: Bearer $KIDSTORY_PARTNER_KEY" \
  -H "Content-Type: application/json" \
  -d '{ "topic": "a little frog who is afraid of the dark", "ageRange": "3-5" }'
```

Body:

| Field | Required | Values |
|---|---|---|
| `topic` | yes | What the story should be about, 3–300 characters |
| `category` | no | `adventure`, `bedtime`, `animals`, `friendship`, `fairy-tale`, `funny` |
| `ageRange` | no | `3-5`, `6-8`, `9-12`, `all-ages` |

Leave out `category` or `ageRange` and the agent picks whatever the catalog has fewest
stories in. A run takes 20–60 seconds, so set your HTTP timeout to at least 90 seconds.

Response, `201 Created`:

```json
{
  "story": {
    "slug": "pip-and-the-moonlit-pond",
    "title": "Pip and the Moonlit Pond",
    "emoji": "🐸",
    "excerpt": "...",
    "category": "bedtime",
    "ageRange": "3-5",
    "readTimeMinutes": 2,
    "tags": ["frog", "night"],
    "body": ["...", "...", "..."],
    "coverImageUrl": "https://...",
    "publishedAt": "2026-10-17T12:00:00Z",
    "isPremium": true
  },
  "url": "https://storyland-sigma.vercel.app/stories/pip-and-the-moonlit-pond",
  "notes": "One sentence from the agent on why it wrote the story this way."
}
```

Python:

```python
import httpx

response = httpx.post(
    "https://kidstory-api-orcin.vercel.app/partner/stories",
    headers={"Authorization": f"Bearer {KIDSTORY_PARTNER_KEY}"},
    json={"topic": "a little frog who is afraid of the dark"},
    timeout=120,
)
response.raise_for_status()
print(response.json()["url"])
```

## Option 2: MCP

If your agent speaks the Model Context Protocol, connect it to:

```
https://kidstory-api-orcin.vercel.app/partner/mcp
```

The transport is streamable HTTP. Send your key as `Authorization: Bearer ksk_...` on
every request; any MCP client that lets you set request headers works. Tools:

| Tool | What it does |
|---|---|
| `list_categories` | The six categories |
| `list_stories` | Existing stories, newest first; filter by `category`, `age_range`, `tag` or search `q` |
| `get_story` | One story by slug |
| `create_story` | `topic` (plus optional `category`, `age_range`): the story agent writes and publishes a story, and returns it with its URL and notes |

`create_story` is the same as the REST call: same limit, same rules, same response.

## Errors

Errors come back as `{ "detail": "..." }` (REST) or as an MCP tool error with the same
message.

| Status | Meaning |
|---|---|
| 401 | Key missing, wrong or revoked |
| 422 | The topic was invalid, or the agent declined it as not right for children. Try a different topic |
| 429 | Today's limit is used up; it resets at 00:00 UTC. Also returned when Kidstory's own AI provider is busy: try again in a minute |
| 502 | The agent couldn't finish a story this time; try again |
| 503 | Story writing is switched off on the server |

## Good topics

Short and concrete works best: "a shy hedgehog at a new pond", "a kite that wants to see
the sea", "two friends who build a snow castle". The topic is a suggestion, not
instructions: the agent keeps Kidstory's own voice, length and rules whatever the topic
says, and it declines anything unsuitable for young children.
