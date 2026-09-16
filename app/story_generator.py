"""Story drafting with the Kidstory fine-tune (ziaflutter/tinyllama-cat-mouse-story).

The model runs behind an OpenAI-compatible server — the llama.cpp Space in
deploy/model-space/ — and is called through huggingface_hub's InferenceClient,
which speaks that protocol when given a `base_url`.

It is a 1.1B prose model, not a JSON one: it returns story text and nothing
else. Everything else the catalog needs (title, excerpt, emoji, tags, read
time) is derived here from that text, so the response has the exact shape the
/studio editor and POST /stories already work with.
"""

import re
from functools import lru_cache

from huggingface_hub import AsyncInferenceClient

from .config import get_settings
from .schemas import ChatMessage, StoryDraft

# The base chat model was tuned with a system turn, so a short one is safe; the
# actual steering happens in the user message, which matches the SFT format.
SYSTEM_PROMPT = (
    "You are a storyteller for young children. Write short, warm, gentle stories "
    "of two or three short paragraphs, with a kind main character and a happy "
    "ending. Nothing scary or violent. Reply with the story text only."
)

# The studio keeps the last few turns so "make it shorter" style follow-ups
# reach the model with the story it wrote before. Keep that bounded: this model
# has a 2048-token context and a story is ~400 tokens.
MAX_HISTORY_TURNS = 4
TEMPERATURE = 0.8

# Words per minute a young reader (or a parent reading aloud) gets through.
WORDS_PER_MINUTE = 120

CATEGORY_EMOJI = {
    "adventure": "🗺️",
    "bedtime": "🌙",
    "animals": "🐾",
    "friendship": "🤝",
    "fairy-tale": "🧚",
    "funny": "😄",
}

# Ordered: earlier entries win when several match. Each is (regex, emoji, tag).
KEYWORD_HINTS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(p, re.IGNORECASE), emoji, tag)
    for p, emoji, tag in [
        (r"\b(kitten|kitty|cat)s?\b", "🐱", "cat"),
        (r"\b(mouse|mice)\b", "🐭", "mouse"),
        (r"\b(puppy|puppies|dog)s?\b", "🐶", "dog"),
        (r"\b(bunny|bunnies|rabbit)s?\b", "🐰", "rabbit"),
        (r"\b(bear)s?\b", "🐻", "bear"),
        (r"\b(fox|foxes)\b", "🦊", "fox"),
        (r"\b(owl)s?\b", "🦉", "owl"),
        (r"\b(bird|robin|sparrow)s?\b", "🐦", "bird"),
        (r"\b(duck|duckling)s?\b", "🦆", "duck"),
        (r"\b(frog)s?\b", "🐸", "frog"),
        (r"\b(turtle|tortoise)s?\b", "🐢", "turtle"),
        (r"\b(elephant)s?\b", "🐘", "elephant"),
        (r"\b(lion)s?\b", "🦁", "lion"),
        (r"\b(dragon)s?\b", "🐉", "dragon"),
        (r"\b(unicorn)s?\b", "🦄", "unicorn"),
        (r"\b(fish|whale|dolphin)e?s?\b", "🐟", "ocean"),
        (r"\b(kite)s?\b", "🪁", "kite"),
        (r"\b(moon|star|night|sleep|dream)s?\b", "🌙", "night"),
        (r"\b(rain|cloud|rainbow)s?\b", "🌈", "weather"),
        (r"\b(garden|flower|tree|forest)s?\b", "🌳", "nature"),
        (r"\b(school|teacher|class)\b", "🏫", "school"),
        (r"\b(friend|friends|friendship)\b", "🤝", "friendship"),
        (r"\b(brave|courage|bravery)\b", "💪", "courage"),
        (r"\b(kind|kindness|share|sharing)\b", "💛", "kindness"),
        (r"\b(magic|magical|wizard|fairy)\b", "✨", "magic"),
        (r"\b(adventure|journey|explore)\b", "🗺️", "adventure"),
    ]
]

# Leading instruction phrasing to strip when a topic is turned into a title:
# "Write me a short bedtime story about a brave mouse" -> "a brave mouse".
TOPIC_PREFIX = re.compile(
    r"^(?:please\s+)?(?:(?:write|tell|create|make|give|generate)\s+(?:me\s+|us\s+)?)?"
    r"(?:(?:a|an|the|another)\s+)?(?:(?:short|little|quick|new|gentle|sweet|funny)\s+)?"
    r"(?:(?:bedtime|children'?s|kids'?|kid'?s)\s+)?(?:story|tale)\s+(?:about|of|where|in which|for)\s+",
    re.IGNORECASE,
)
SMALL_WORDS = {"a", "an", "and", "the", "of", "in", "on", "at", "to", "for", "with"}
INSTRUCTION_START = re.compile(r"^(?:please\s+)?(?:write|tell|create|make|give|generate|revise|change|rewrite)\b", re.IGNORECASE)

SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class StoryGenerationError(Exception):
    """A failure the HTTP layer turns into a response; `status_code` says which."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@lru_cache
def _client() -> AsyncInferenceClient:
    settings = get_settings()
    return AsyncInferenceClient(
        base_url=settings.story_model_url,
        api_key=settings.story_model_api_key or None,
        timeout=settings.story_model_timeout,
    )


async def _complete(messages: list[dict[str, str]]) -> str:
    """One chat completion against the model server; returns the raw text."""
    settings = get_settings()
    if not settings.story_model_url:
        raise StoryGenerationError(503, "Story generation is not configured on this server (STORY_MODEL_URL is unset).")
    try:
        completion = await _client().chat_completion(
            messages,
            model=settings.story_model_name,
            max_tokens=settings.story_model_max_tokens,
            temperature=TEMPERATURE,
            top_p=0.9,
            # The fine-tune occasionally runs on into the next turn marker
            # instead of emitting EOS; cut it there rather than shipping garbage.
            stop=["<|user|>", "<|system|>", "<|assistant|>"],
        )
    except Exception as exc:  # noqa: BLE001 - any transport/HTTP failure is a 502 to our caller
        raise StoryGenerationError(502, f"The story model could not be reached: {exc}") from exc
    if not completion.choices:
        return ""
    content = (completion.choices[0].message.content or "").strip()
    if completion.choices[0].finish_reason == "length":
        content = _trim_to_last_sentence(content)
    return content


def _trim_to_last_sentence(text: str) -> str:
    """A reply cut off by the token budget ends mid-sentence; drop the fragment."""
    cut = max(text.rfind(end) for end in (".", "!", "?", "\u201d", '"'))
    return text[: cut + 1] if cut > 0 else text


def _build_user_message(topic: str, category: str | None, age_range: str | None) -> str:
    """The user turn, in the "Write a short story about ..." form the SFT data used.

    A topic that is already an instruction ("make it shorter", "write a story
    where...") is passed through as-is rather than wrapped a second time.
    """
    topic = topic.strip().rstrip(".")
    request = topic if INSTRUCTION_START.match(topic) else f"Write a short story about {topic}"
    hints = []
    if category:
        label = category.replace("-", " ")
        hints.append(f"Make it {'an' if label[0] in 'aeiou' else 'a'} {label} story.")
    if age_range and age_range != "all-ages":
        hints.append(f"It is for children aged {age_range}.")
    return " ".join([f"{request}.", *hints])


def _split_paragraphs(text: str) -> list[str]:
    """Paragraphs from the model's prose; re-chunks a single wall of text."""
    text = text.replace("\r\n", "\n")
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    paragraphs = [p for p in paragraphs if p]
    if len(paragraphs) != 1:
        return paragraphs
    sentences = SENTENCE_END.split(paragraphs[0])
    if len(sentences) < 6:
        return paragraphs
    # Three roughly equal groups of sentences reads like the site's stories.
    per = -(-len(sentences) // 3)
    return [" ".join(sentences[i : i + per]) for i in range(0, len(sentences), per)]


def _pop_title(paragraphs: list[str]) -> tuple[str | None, list[str]]:
    """If the model led with a title line, take it off the top of the body."""
    if not paragraphs:
        return None, paragraphs
    # Markdown emphasis ("**The Brave Mouse**") is common from larger models.
    first = paragraphs[0].strip().strip("*_ ").strip()
    match = re.match(r"^(?:#+\s*|title:\s*)?[\"“]?(.+?)[\"”]?$", first, re.IGNORECASE)
    candidate = match.group(1).strip() if match else first
    looks_like_title = (
        len(paragraphs) > 1
        and len(candidate.split()) <= 10
        and not candidate.endswith((".", "!", "?", ","))
    ) or first.lower().startswith(("title:", "#"))
    if looks_like_title:
        return candidate.rstrip(".") or None, paragraphs[1:]
    return None, paragraphs


def _title_from_topic(topic: str) -> str:
    stripped = TOPIC_PREFIX.sub("", topic.strip()).strip(" .!?\"'")
    words = (stripped or topic).split()[:10]
    titled = [w if (i and w.lower() in SMALL_WORDS) else w[:1].upper() + w[1:] for i, w in enumerate(words)]
    return " ".join(titled)[:80] or "A New Story"


def _first_sentence(paragraph: str) -> str:
    sentence = SENTENCE_END.split(paragraph, maxsplit=1)[0].strip()
    return sentence[:200] if len(sentence) <= 200 else sentence[:197].rstrip() + "..."


def _pick_emoji_and_tags(topic: str, text: str, category: str) -> tuple[str, list[str]]:
    """Emoji from whatever the topic names first, else the story; tags from both."""
    emoji: str | None = None
    tags: list[str] = []
    for source in (topic, text):
        for pattern, hint_emoji, tag in KEYWORD_HINTS:
            if pattern.search(source):
                emoji = emoji or hint_emoji
                if tag not in tags and tag != category:
                    tags.append(tag)
    tags = tags[:3]
    if len(tags) < 2 and category not in tags:
        tags.append(category)
    return emoji or CATEGORY_EMOJI.get(category, "✨"), tags[:3]


def _read_time_minutes(body: list[str]) -> int:
    words = sum(len(p.split()) for p in body)
    return max(1, min(3, round(words / WORDS_PER_MINUTE) or 1))


def draft_from_text(text: str, *, topic: str, category: str | None, age_range: str | None) -> StoryDraft:
    """Turns the model's prose into the metadata-complete draft the catalog expects."""
    title, body = _pop_title(_split_paragraphs(text))
    if not body:
        raise StoryGenerationError(502, "The story model returned an empty story. Please try again.")
    full_text = " ".join(body)
    # No category requested: call it an animal story if it plainly is one.
    resolved_category = category or ("animals" if any(
        p.search(full_text) for p, _, tag in KEYWORD_HINTS[:16]
    ) else "adventure")
    emoji, tags = _pick_emoji_and_tags(topic, full_text, resolved_category)
    return StoryDraft(
        title=title or _title_from_topic(topic),
        emoji=emoji,
        excerpt=_first_sentence(body[0]),
        category=resolved_category,
        age_range=age_range or "all-ages",
        read_time_minutes=_read_time_minutes(body),
        tags=tags,
        body=body,
    )


async def generate_story_draft(
    topic: str,
    *,
    category: str | None = None,
    age_range: str | None = None,
    history: list[ChatMessage] | None = None,
) -> tuple[StoryDraft, list[ChatMessage]]:
    """Generates a draft and returns it with the updated conversation history."""
    prior = [m for m in (history or []) if m.content.strip()][-MAX_HISTORY_TURNS:]
    user_message = _build_user_message(topic, category, age_range)

    # A follow-up like "make it shorter" is a revision, not a subject: title the
    # draft after the request that started the conversation instead.
    title_topic = topic
    if prior and INSTRUCTION_START.match(topic.strip()) and not TOPIC_PREFIX.match(topic.strip()):
        first_request = next((m.content for m in prior if m.role == "user"), None)
        if first_request:
            title_topic = _first_sentence(first_request)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *({"role": m.role, "content": m.content} for m in prior),
        {"role": "user", "content": user_message},
    ]

    # A small model occasionally emits nothing but a stop token; one retry
    # absorbs that without surfacing an error for a transient blip.
    last_error: StoryGenerationError | None = None
    for _ in range(2):
        try:
            text = await _complete(messages)
            draft = draft_from_text(text, topic=title_topic, category=category, age_range=age_range)
        except StoryGenerationError as exc:
            if exc.status_code == 503:
                raise
            last_error = exc
            continue
        turn = [ChatMessage(role="user", content=user_message), ChatMessage(role="assistant", content=text)]
        return draft, [*prior, *turn]

    assert last_error is not None
    raise last_error
