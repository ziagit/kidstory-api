"""The loop every agent shares: Claude, a set of tools, and the SDK's tool runner.

The tool runner sends the request, runs whichever tools Claude calls, feeds the
results back and repeats until Claude answers without a tool call (or the
iteration cap is hit). Each agent supplies its own prompt and tools.

Tools can include Anthropic's server tools (web search): those run inside one
Claude call, and a long one can stop with `pause_turn`. The Python runner
doesn't resume that, so the loop restarts it from the history it mirrors.
"""

from collections.abc import Callable, Sequence
from functools import lru_cache
from typing import Any

import anthropic

from ..config import get_settings
from ..schemas import ChatMessage

# The reply budget covers Claude's thinking as well as its text and tool calls.
# A short story at low effort uses a small part of it.
MAX_TOKENS = 16000
# Per Claude call. The API's Vercel function stops at 300s (vercel.json), and a
# run makes a handful of calls, most of them short tool turns. The books agent's
# calls run web searches and get longer (its own TIMEOUT_SECONDS).
TIMEOUT_SECONDS = 50.0
MAX_RETRIES = 1
# A server-tool turn that pauses (`pause_turn`) is resumed at most this many times.
MAX_PAUSE_RESUMES = 3
# Opts into Anthropic's server-side fallback: if a safety classifier declines
# a (benign) request, the API retries it on the model it recommends instead.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AgentError(Exception):
    """A failure the HTTP layer turns into a response; `status_code` says which."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@lru_cache
def _client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=get_settings().anthropic_api_key,
        timeout=TIMEOUT_SECONDS,
        max_retries=MAX_RETRIES,
    )


async def run_agent(
    *,
    system: str,
    prompt: str,
    tools: Sequence[Any],
    max_iterations: int,
    history: Sequence[ChatMessage] = (),
    effort: str = "low",
    timeout: float = TIMEOUT_SECONDS,
    on_message: Callable[[Any], None] | None = None,
) -> str:
    """Runs the agent to completion and returns its final text reply.

    `history` is earlier turns of a conversation (plain text), sent ahead of
    `prompt` so a follow-up reaches Claude with what came before it.
    `timeout` is per Claude call; raise it for agents whose calls run server
    tools. `on_message` sees each response before its tool calls run, e.g. to
    collect the URLs a web search returned.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise AgentError(503, "Agents are not configured on this server (ANTHROPIC_API_KEY is unset).")

    turns = [{"role": m.role, "content": m.content} for m in history if m.content.strip()]
    # The conversation must open on a user turn; trimmed history may not.
    while turns and turns[0]["role"] != "user":
        turns = turns[1:]

    client = _client() if timeout == TIMEOUT_SECONDS else _client().with_options(timeout=timeout)
    # Mirrored so a paused turn can be resumed: the runner keeps its own copy.
    messages: list[Any] = [*turns, {"role": "user", "content": prompt}]

    last = None
    try:
        for _ in range(MAX_PAUSE_RESUMES + 1):
            runner = client.beta.messages.tool_runner(
                model=settings.anthropic_model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=list(messages),
                tools=list(tools),
                max_iterations=max_iterations,
                output_config={"effort": effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
            async for message in runner:
                last = message
                if on_message is not None:
                    on_message(message)
                if message.stop_reason == "refusal":
                    break
                messages.append({"role": "assistant", "content": message.content})
                # Cached by the runner, so each tool still runs once.
                tool_response = await runner.generate_tool_call_response()
                if tool_response is not None:
                    messages.append(tool_response)
            if last is None or last.stop_reason != "pause_turn":
                break
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise AgentError(503, f"Claude rejected this server's ANTHROPIC_API_KEY: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        raise AgentError(429, "Claude is rate-limiting this server. Please try again in a minute.") from exc
    except anthropic.APIStatusError as exc:
        raise AgentError(502, f"Claude returned an error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AgentError(502, f"Claude could not be reached: {exc}") from exc

    if last is None:
        raise AgentError(502, "The agent stopped before Claude replied.")
    if last.stop_reason == "refusal":
        raise AgentError(422, "Claude declined this task.")
    return "".join(block.text for block in last.content if block.type == "text").strip()
