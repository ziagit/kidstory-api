"""The loop both agents share: Claude, a set of tools, and the SDK's tool runner.

The tool runner sends the request, runs whichever tools Claude calls, feeds the
results back and repeats until Claude answers without a tool call (or the
iteration cap is hit). Each agent supplies its own prompt and tools.
"""

from collections.abc import Sequence
from typing import Any

import anthropic

from ..config import get_settings
from ..story_generator import FALLBACK_BETA, MAX_TOKENS, StoryGenerationError, _client


class AgentError(StoryGenerationError):
    """A failure the HTTP layer turns into a response; `status_code` says which."""


async def run_agent(
    *,
    system: str,
    prompt: str,
    tools: Sequence[Any],
    max_iterations: int,
    effort: str = "low",
) -> str:
    """Runs the agent to completion and returns its final text reply."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise AgentError(503, "Agents are not configured on this server (ANTHROPIC_API_KEY is unset).")

    runner = _client().beta.messages.tool_runner(
        model=settings.anthropic_model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        tools=list(tools),
        max_iterations=max_iterations,
        output_config={"effort": effort},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )

    last = None
    try:
        async for message in runner:
            last = message
            if message.stop_reason == "refusal":
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
