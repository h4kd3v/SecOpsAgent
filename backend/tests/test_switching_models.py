"""Changing LLM_MODEL_NAME between turns.

An operator swaps the model in .env and restarts. Everything answered before
that was answered by a different model, and the transcript has to keep saying
so — a security tool whose history quietly rewrites itself is worse than one
with no history.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="needs a throwaway Postgres"
)

import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import agent_loop, llm  # noqa: E402
from app.services.mcp_manager import ToolSpec  # noqa: E402

settings = get_settings()
READ_TOOL = ToolSpec("udm_search", "search", {"type": "object"}, read_only=True)


class FakeMcp:
    last_error = None

    async def list_tools(self):
        return [READ_TOOL]


@pytest.fixture
async def stack(monkeypatch):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    from app.services import tool_catalog as catalog_module

    monkeypatch.setattr(agent_loop, "mcp_manager", FakeMcp())
    monkeypatch.setattr(catalog_module, "mcp_manager", FakeMcp())
    yield
    await engine.dispose()


def serving(model_id: str, rate: str | None = None):
    """A gateway that answers as `model_id`."""

    async def answer(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_token("Checked.")
        result = llm.StreamedTurn()
        result.content = "Checked."
        result.model = model_id
        result.usage = {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 0,
            "total_tokens": 1_000_000,
        }
        return result

    return answer


async def analyst() -> httpx.AsyncClient:
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await client.post("/api/session")
    return client


async def _ask(client, conversation_id: str, text: str) -> None:
    async with client.stream(
        "POST", f"/api/conversations/{conversation_id}/messages", json={"message": text}
    ) as response:
        async for _ in response.aiter_bytes():
            pass


async def test_each_turn_keeps_the_model_that_served_it(stack, monkeypatch):
    """The transcript is a record of what happened, not of what is configured
    now."""
    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]

        monkeypatch.setattr(settings, "llm_model_name", "gpt-4.1")
        monkeypatch.setattr(llm, "stream_completion", serving("gpt-4.1"))
        await _ask(alice, created, "first, on the old model")

        # The operator edits .env and restarts.
        monkeypatch.setattr(settings, "llm_model_name", "claude-opus-5")
        monkeypatch.setattr(llm, "stream_completion", serving("claude-opus-5"))
        await _ask(alice, created, "second, on the new one")

        answers = [
            m["model"]
            for m in (await alice.get(f"/api/conversations/{created}")).json()["messages"]
            if m["role"] == "assistant"
        ]
        assert answers == ["gpt-4.1", "claude-opus-5"]
    finally:
        await alice.aclose()


async def test_the_ui_is_told_which_model_is_configured(stack, monkeypatch):
    """Without it the frontend cannot tell an answer from the current model
    apart from one an earlier model produced, and the display-name label ends
    up pasted over both."""
    monkeypatch.setattr(settings, "llm_model_name", "claude-opus-5")
    monkeypatch.setattr(settings, "llm_model_display_name", "Claude Opus 5")
    alice = await analyst()
    try:
        config = (await alice.get("/api/config")).json()
        assert config["model_name"] == "claude-opus-5"
        assert config["model_display_name"] == "Claude Opus 5"
    finally:
        await alice.aclose()


async def test_switching_models_mid_thread_still_replays_cleanly(stack, monkeypatch):
    """History from one model is just messages to the next; the turn after a
    switch has to work rather than choke on the earlier transcript."""
    seen: list[int] = []

    async def counts_history(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        seen.append(len([m for m in messages if m["role"] != "system"]))
        await on_token("ok")
        result = llm.StreamedTurn()
        result.content = "ok"
        result.model = "claude-opus-5"
        return result

    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        monkeypatch.setattr(settings, "llm_model_name", "gpt-4.1")
        monkeypatch.setattr(llm, "stream_completion", serving("gpt-4.1"))
        await _ask(alice, created, "first")

        monkeypatch.setattr(settings, "llm_model_name", "claude-opus-5")
        monkeypatch.setattr(llm, "stream_completion", counts_history)
        await _ask(alice, created, "second")

        assert seen and seen[0] >= 3, "the new model was not given the earlier turns"
    finally:
        await alice.aclose()


async def test_cost_stops_rather_than_guessing_when_a_new_model_has_no_rate(
    stack, monkeypatch
):
    """Switching models without adding a price records nothing, which is the
    honest outcome — the alternative is a cost report billed at the old
    model's rate."""
    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        monkeypatch.setattr(settings, "llm_model_pricing", "gpt-4.1=2.00/8.00")

        monkeypatch.setattr(settings, "llm_model_name", "gpt-4.1")
        monkeypatch.setattr(llm, "stream_completion", serving("gpt-4.1"))
        await _ask(alice, created, "priced")

        monkeypatch.setattr(settings, "llm_model_name", "claude-opus-5")
        monkeypatch.setattr(llm, "stream_completion", serving("claude-opus-5"))
        await _ask(alice, created, "unpriced")

        costs = [
            m["cost_usd"]
            for m in (await alice.get(f"/api/conversations/{created}")).json()["messages"]
            if m["role"] == "assistant"
        ]
        assert costs[0] is not None and float(costs[0]) == 2.0
        assert costs[1] is None, "an unpriced model was billed at another model's rate"
    finally:
        await alice.aclose()


def test_a_stale_display_name_is_flagged():
    """The label is typed by hand and does not follow the model. Left over from
    the previous one, it attributes every new answer to a model that never saw
    them — which is the mislabelling the per-message model id exists to
    prevent, reintroduced by hand."""
    stale = get_settings().model_copy(
        update={"llm_model_name": "claude-opus-4-6", "llm_model_display_name": "GPT-4.1"}
    )
    assert stale.display_name_looks_stale()


def test_a_matching_display_name_is_not_nagged_about():
    """Loose on purpose: a label is prose, not an id, and warning about
    "Claude Opus 4.6" on `claude-opus-4-6` would train people to ignore it."""
    for model, label in [
        ("claude-opus-4-6", "Claude Opus 4.6"),
        ("gpt-4.1", "GPT-4.1"),
        ("gemini-2.5-pro", "Gemini 2.5 Pro"),
        ("gpt-4.1", ""),
    ]:
        settings = get_settings().model_copy(
            update={"llm_model_name": model, "llm_model_display_name": label}
        )
        assert not settings.display_name_looks_stale(), f"{label!r} vs {model!r}"
