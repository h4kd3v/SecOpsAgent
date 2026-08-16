"""Thin wrapper over the OpenAI-compatible LLM proxy.

Everything provider-specific lives here so swapping proxies is a one-file change.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """Built on first use, not at import.

    Constructing it eagerly makes importing this module fail outright when the
    proxy isn't configured yet — which takes the whole app down at startup
    rather than failing the one request that actually needed it.
    """
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=settings.llm_proxy_url or "http://llm-proxy-not-configured/v1",
            api_key=settings.llm_api_key or "not-configured",
            timeout=settings.llm_request_timeout,
            max_retries=2,
        )
    return _client


def describe_error(exc: Exception) -> str:
    """Turn a provider exception into a sentence an analyst can act on.

    Gateways return their diagnostics as a wall of nested JSON. Dumping that
    into the chat tells the reader nothing about whether to retry, tell an
    admin, or change a setting — so the common cases get named explicitly and
    the raw text is kept as a suffix for whoever needs it.
    """
    status = getattr(exc, "status_code", None)
    code = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error") or {}
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or "")

    raw = str(exc)
    model = settings.llm_model_name

    if code == "insufficient_quota" or (status == 429 and "quota" in raw.lower()):
        return (
            "The LLM account has no remaining credit, so the request was rejected "
            "before the model ran. Add billing to the account that issued "
            "LLM_API_KEY, or point LLM_PROXY_URL at a gateway that has credit. "
            "This is not a fault in the app — MCP tools are unaffected."
        )
    if status == 429:
        return (
            "The LLM gateway is rate-limiting this key. Wait a moment and retry; "
            "if it persists, lower RATE_LIMIT_MESSAGES_PER_MINUTE or raise the "
            "quota on the gateway."
        )
    if status == 401 or code in ("invalid_api_key", "authentication_error"):
        return (
            "The LLM gateway rejected LLM_API_KEY. Check the key, and that it "
            f"belongs to the endpoint configured in LLM_PROXY_URL "
            f"({settings.llm_proxy_url})."
        )
    if status == 404 or code == "model_not_found":
        return (
            f"The gateway does not offer a model called '{model}'. Check "
            "LLM_MODEL_NAME against the gateway's model list "
            "(`docker compose exec backend python -m app.diagnose`)."
        )
    if code == "context_length_exceeded" or "context length" in raw.lower():
        return (
            f"The conversation exceeded {model}'s context window. Lower "
            "LLM_MAX_CONTEXT_CHARS, set TOOL_RESULT_MAX_CHARS, or narrow "
            "TOOL_ALLOWLIST — the SecOps tool schemas alone are large."
        )
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in raw.lower():
        return (
            f"The LLM gateway did not respond within {settings.llm_request_timeout:.0f}s. "
            "Raise LLM_REQUEST_TIMEOUT if the model is genuinely slow."
        )
    if status and 500 <= status < 600:
        return f"The LLM gateway returned a {status}. That's an upstream fault; retry shortly."

    return f"The LLM call failed: {type(exc).__name__}. {raw[:300]}"


@dataclass
class StreamedTurn:
    """What one assistant turn produced once the stream is exhausted."""

    content: str = ""
    # Analysis the model emitted on its way to the answer. Gateways expose it
    # as `reasoning_content` (LiteLLM, DeepSeek) or `reasoning`; it is not part
    # of the answer and is never sent back as assistant content.
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    # What the gateway actually served. LiteLLM resolves an alias to a concrete
    # upstream model, and that resolved name is the one worth recording.
    model: str | None = None


class _ToolCallAccumulator:
    """Streaming deltas arrive fragmented and keyed by index, not id:

        delta.tool_calls[0].id            -> "call_abc"    (first chunk only)
        delta.tool_calls[0].function.name -> "secops_sea"  (may be split)
        delta.tool_calls[0].function.arguments -> '{"qu'   (always split)

    Anything that reads `chunk.tool_calls` directly gets garbage.
    """

    def __init__(self) -> None:
        self._by_index: dict[int, dict[str, Any]] = {}

    def add(self, deltas: Any) -> None:
        for delta in deltas or []:
            slot = self._by_index.setdefault(
                delta.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if delta.id:
                slot["id"] = delta.id
            fn = getattr(delta, "function", None)
            if fn is None:
                continue
            if fn.name:
                slot["function"]["name"] += fn.name
            if fn.arguments:
                slot["function"]["arguments"] += fn.arguments

    def result(self) -> list[dict[str, Any]]:
        return [self._by_index[i] for i in sorted(self._by_index)]


async def stream_completion(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    on_token: Any,
    on_reasoning: Any = None,
) -> StreamedTurn:
    """Run one completion, invoking `on_token(text)` per text delta.

    `on_reasoning` receives the model's analysis deltas as they arrive, so the
    analyst watches the working rather than a spinner. Optional because not
    every gateway or model emits any.
    """
    kwargs: dict[str, Any] = {
        "model": settings.llm_model_name,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    # Deliberately unset by default: capping output tokens truncates answers
    # mid-sentence, which is indistinguishable from a broken stream.
    if settings.llm_max_output_tokens:
        kwargs["max_tokens"] = settings.llm_max_output_tokens

    turn = StreamedTurn()
    accumulator = _ToolCallAccumulator()

    stream = await get_client().chat.completions.create(**kwargs)
    async for chunk in stream:
        if chunk.usage:
            turn.usage = chunk.usage.model_dump()
        if chunk.model and not turn.model:
            turn.model = chunk.model
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if choice.finish_reason:
            turn.finish_reason = choice.finish_reason
        delta = choice.delta
        if delta is None:
            continue
        if delta.content:
            turn.content += delta.content
            await on_token(delta.content)
        # Not a typed field on the OpenAI SDK's delta: gateways add it, so it
        # arrives via pydantic's extras. Two spellings are in the wild.
        thought = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
        if thought and on_reasoning is not None:
            turn.reasoning += thought
            await on_reasoning(thought)
        if delta.tool_calls:
            accumulator.add(delta.tool_calls)

    turn.tool_calls = [tc for tc in accumulator.result() if tc["function"]["name"]]
    return turn


async def generate_title(first_user_message: str, first_reply: str) -> str:
    """One cheap call to name the thread, ChatGPT-sidebar style."""
    try:
        response = await get_client().chat.completions.create(
            model=settings.title_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Write a 3-6 word title for this security investigation. "
                        "No quotes, no trailing period."
                    ),
                },
                {"role": "user", "content": f"{first_user_message}\n\n{first_reply[:600]}"},
            ],
            max_tokens=24,
            temperature=0.2,
        )
        title = (response.choices[0].message.content or "").strip().strip('"')
        return title[:120] or "New conversation"
    except Exception:  # noqa: BLE001 - a title is never worth failing a turn over
        logger.warning("title generation failed", exc_info=True)
        return "New conversation"


async def ping() -> bool:
    try:
        await get_client().models.list()
        return True
    except Exception:  # noqa: BLE001
        return False


SYSTEM_PROMPT = """You are SecOps Agent, an assistant for security operations analysts.

You have tools backed by the organisation's SecOps platform. Use them to ground \
every factual claim — never invent hostnames, IPs, rule names, case IDs, alert \
counts, or timestamps. If a tool returns nothing, say so plainly.

Guidelines:
- Prefer one precise query over several broad ones. State the time window you used.
- Show the key evidence (fields, counts, timestamps) that supports your conclusion.
- Distinguish clearly between what the data shows and what you infer from it.
- Actions that change state require the analyst's approval; explain what a change \
would do and why before requesting it.
- Format for fast reading: short paragraphs, tables for row-shaped data, and \
fenced code blocks for queries and raw records."""


def estimate_usage(
    messages: list[dict[str, Any]], completion: str, tool_calls: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Fallback when the gateway omits usage from a streamed response.

    Not every proxy honours `stream_options.include_usage`, and showing nothing
    looks like a bug. A ~4-chars-per-token heuristic is rough but the right
    order of magnitude; it is flagged `estimated` so the UI can mark it with a
    `~` rather than passing a guess off as a measurement.
    """
    prompt_chars = sum(_approx_size(m) for m in messages)
    completion_chars = len(completion or "")
    if tool_calls:
        completion_chars += len(json.dumps(tool_calls))
    prompt_tokens = prompt_chars // 4
    completion_tokens = completion_chars // 4
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "estimated": True,
    }


def _approx_size(message: dict[str, Any]) -> int:
    size = len(message.get("content") or "")
    if message.get("tool_calls"):
        size += len(json.dumps(message["tool_calls"]))
    return size


async def build_messages(
    history: list[dict[str, Any]],
    max_messages: int,
    max_chars: int | None = None,
) -> list[dict[str, Any]]:
    """Trim replayed history to a budget without orphaning tool messages.

    Two budgets, because either alone is wrong:

    *   **Message count** stops a long conversation from growing unbounded.
    *   **Character count** is what actually protects the context window. A
        single UDM search can return half a megabyte, so forty messages is not
        a meaningful limit.

    Trimming happens from the oldest end. Because we only ever drop a prefix,
    an assistant turn we keep always keeps its `tool` replies too — the
    remaining hazard is the mirror image, a `tool` message whose parent
    assistant turn was dropped, which makes the proxy reject the request. Those
    are shed explicitly.

    Note this trims *replay*: a tool result is always passed to the model in
    full on the turn it was fetched. It only comes under budget pressure on
    later turns, by which point the model has already read it.
    """
    budget = settings.llm_max_context_chars if max_chars is None else max_chars
    window = history[-max_messages:] if len(history) > max_messages else list(history)

    if budget > 0:
        total = sum(_approx_size(m) for m in window)
        while len(window) > 1 and total > budget:
            total -= _approx_size(window.pop(0))

    while window and window[0].get("role") == "tool":
        window.pop(0)
    return [{"role": "system", "content": SYSTEM_PROMPT}, *window]
