"""Cost, feedback, pins and tags.

The through-line: each of these is a number or a label someone will make a
decision on, so the failure that matters is not "it is missing" but "it is
confidently wrong".
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="needs a throwaway Postgres"
)

import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import Base, Conversation  # noqa: E402
from app.db.session import SessionMaker, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import agent_loop, llm, pricing, repository as repo  # noqa: E402
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
    monkeypatch.setattr(settings, "llm_model_name", "gpt-4.1")
    monkeypatch.setattr(settings, "llm_model_pricing", "gpt-4.1=2.00/8.00")

    async def answers(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_token("Checked.")
        result = llm.StreamedTurn()
        result.content = "Checked."
        result.model = "gpt-4.1"
        result.usage = {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 1_000_000,
            "total_tokens": 2_000_000,
        }
        return result

    monkeypatch.setattr(llm, "stream_completion", answers)
    yield
    await engine.dispose()


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


# --- cost ---------------------------------------------------------------

def test_a_model_with_no_configured_rate_records_no_cost(monkeypatch):
    """A missing price is a gap an operator can see and fill. A guessed one is
    a number that looks authoritative and is wrong."""
    monkeypatch.setattr(settings, "llm_model_pricing", "gpt-4.1=2.00/8.00")
    assert pricing.cost_for("some-other-model", {"prompt_tokens": 1000}) is None


def test_a_versioned_model_id_matches_its_configured_family(monkeypatch):
    """Gateways resolve aliases to dated ids; the rate should still apply."""
    monkeypatch.setattr(settings, "llm_model_pricing", "gpt-4.1=2.00/8.00")
    assert pricing.rates_for("gpt-4.1-2025-04-14") == (2.00, 8.00)


def test_the_longest_matching_prefix_wins(monkeypatch):
    """`gpt-4.1-mini` must not be billed at `gpt-4.1` rates."""
    monkeypatch.setattr(
        settings, "llm_model_pricing", "gpt-4.1=2.00/8.00,gpt-4.1-mini=0.40/1.60"
    )
    assert pricing.rates_for("gpt-4.1-mini-2025-04-14") == (0.40, 1.60)


def test_fractions_of_a_cent_are_not_rounded_away(monkeypatch):
    """A month of cheap turns is real money; rounding each to zero loses it."""
    monkeypatch.setattr(settings, "llm_model_pricing", "cheap=0.10/0.10")
    cost = pricing.cost_for("cheap", {"prompt_tokens": 100, "completion_tokens": 0})
    assert cost == Decimal("0.000010")


async def test_a_turn_records_what_it_cost_and_at_what_rate(stack):
    """The rate is stored with the amount, so changing prices later never
    rewrites what past turns cost."""
    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "how much?")

        detail = (await alice.get(f"/api/conversations/{created}")).json()
        answer = [m for m in detail["messages"] if m["role"] == "assistant"][-1]

        # 1M prompt at $2 + 1M completion at $8.
        assert Decimal(answer["cost_usd"]) == Decimal("10.000000")
        assert answer["token_usage"]["input_rate_per_1m"] == 2.00
        assert answer["token_usage"]["output_rate_per_1m"] == 8.00
        assert Decimal(detail["conversation"]["cost_usd"]) == Decimal("10.000000")
    finally:
        await alice.aclose()


async def test_repricing_does_not_rewrite_history(stack, monkeypatch):
    """What was spent is a fact about the past."""
    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "first")

        monkeypatch.setattr(settings, "llm_model_pricing", "gpt-4.1=99.00/99.00")

        detail = (await alice.get(f"/api/conversations/{created}")).json()
        answer = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
        assert Decimal(answer["cost_usd"]) == Decimal("10.000000")
    finally:
        await alice.aclose()


# --- feedback -----------------------------------------------------------

async def test_several_analysts_can_rate_the_same_answer(stack):
    """The point of feedback in a shared workspace: agreement, or the lack of
    it, across the people who actually read the answer."""
    alice, bob = await analyst(), await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "was this useful?")
        detail = (await alice.get(f"/api/conversations/{created}")).json()
        answer_id = [m for m in detail["messages"] if m["role"] == "assistant"][-1]["id"]
        url = f"/api/conversations/{created}/messages/{answer_id}/feedback"

        assert (await alice.put(url, json={"rating": "up"})).json()["up"] == 1
        tally = (await bob.put(url, json={"rating": "down", "note": "missed a host"})).json()

        assert tally == {"up": 1, "down": 1, "mine": "down"}
        seen_by_alice = [
            m
            for m in (await alice.get(f"/api/conversations/{created}")).json()["messages"]
            if m["id"] == answer_id
        ][0]
        assert seen_by_alice["my_feedback"] == "up", "alice was shown bob's vote as her own"
        assert seen_by_alice["feedback_down"] == 1
    finally:
        await alice.aclose()
        await bob.aclose()


async def test_one_analyst_gets_one_vote_and_can_change_it(stack):
    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "rate me")
        detail = (await alice.get(f"/api/conversations/{created}")).json()
        answer_id = [m for m in detail["messages"] if m["role"] == "assistant"][-1]["id"]
        url = f"/api/conversations/{created}/messages/{answer_id}/feedback"

        await alice.put(url, json={"rating": "up"})
        await alice.put(url, json={"rating": "up"})
        changed = (await alice.put(url, json={"rating": "down"})).json()

        assert changed == {"up": 0, "down": 1, "mine": "down"}
        assert (await alice.delete(url)).json() == {"up": 0, "down": 0, "mine": None}
    finally:
        await alice.aclose()


async def test_a_prompt_cannot_be_rated(stack):
    """Rating your own question is meaningless; the endpoint says so rather
    than storing something nobody can interpret."""
    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "my question")
        detail = (await alice.get(f"/api/conversations/{created}")).json()
        prompt_id = [m for m in detail["messages"] if m["role"] == "user"][0]["id"]

        response = await alice.put(
            f"/api/conversations/{created}/messages/{prompt_id}/feedback",
            json={"rating": "up"},
        )
        assert response.status_code == 400
    finally:
        await alice.aclose()


# --- pins and tags ------------------------------------------------------

async def test_pinned_threads_sort_above_newer_ones(stack):
    """The reason pinning exists: twenty analysts sharing one sidebar bury the
    good investigations within a week."""
    alice = await analyst()
    try:
        old = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, old, "the important one")
        new = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, new, "something newer")

        assert [c["id"] for c in (await alice.get("/api/conversations")).json()][0] == new

        await alice.patch(f"/api/conversations/{old}", json={"pinned": True})
        listed = (await alice.get("/api/conversations")).json()

        assert listed[0]["id"] == old
        assert listed[0]["pinned"] is True
    finally:
        await alice.aclose()


async def test_tags_are_cleaned_rather_than_stored_as_typed(stack):
    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "tag me")

        await alice.patch(
            f"/api/conversations/{created}",
            json={"tags": ["  INC-4471 ", "inc-4471", "lateral   movement", "   "]},
        )

        row = (await alice.get("/api/conversations")).json()[0]
        assert row["tags"] == ["INC-4471", "lateral movement"]
    finally:
        await alice.aclose()


async def test_updating_one_field_leaves_the_others_alone(stack):
    """A rename must not silently clear the incident number."""
    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "start")
        await alice.patch(
            f"/api/conversations/{created}", json={"tags": ["INC-1"], "pinned": True}
        )

        await alice.patch(f"/api/conversations/{created}", json={"title": "renamed"})

        row = (await alice.get("/api/conversations")).json()[0]
        assert row["title"] == "renamed"
        assert row["tags"] == ["INC-1"]
        assert row["pinned"] is True
    finally:
        await alice.aclose()


# --- titles -------------------------------------------------------------

async def test_a_thread_is_named_from_its_first_prompt_immediately(stack):
    """Titling used to run after a successful turn, so any thread whose first
    turn failed stayed "New conversation" for good."""
    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "  which hosts   talked to 203.0.113.45?  ")

        row = (await alice.get("/api/conversations")).json()[0]
        assert row["title"] == "which hosts talked to 203.0.113.45?"
    finally:
        await alice.aclose()


async def test_a_thread_whose_first_turn_fails_still_gets_a_name(stack, monkeypatch):
    async def explodes(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        raise RuntimeError("Error code: 429 - insufficient_quota")

    monkeypatch.setattr(llm, "stream_completion", explodes)
    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "list open cases")

        row = (await alice.get("/api/conversations")).json()[0]
        assert row["title"] == "list open cases"
    finally:
        await alice.aclose()


async def test_a_long_prompt_is_trimmed_to_a_sidebar_width(stack):
    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "why " * 60)

        row = (await alice.get("/api/conversations")).json()[0]
        assert len(row["title"]) <= 62
        assert row["title"].endswith("…")
    finally:
        await alice.aclose()


async def test_a_rename_survives_the_next_turn(stack):
    """A title an analyst chose must not be overwritten by later machinery."""
    alice = await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "first question")
        await alice.patch(f"/api/conversations/{created}", json={"title": "INC-4471 triage"})

        await _ask(alice, created, "second question")

        async with SessionMaker() as db:
            import uuid as _uuid

            conversation = await db.get(Conversation, _uuid.UUID(created))
        assert conversation.title == "INC-4471 triage"
    finally:
        await alice.aclose()


def test_title_from_prompt_collapses_pasted_whitespace():
    """A pasted multi-line query would otherwise become a row of newlines."""
    assert repo.title_from_prompt("list\n\n  all   cases\n") == "list all cases"
