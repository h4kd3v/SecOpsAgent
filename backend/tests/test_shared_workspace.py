"""One shared workspace across the shift.

Every analyst sees every investigation. That is a deliberate reversal of the
per-browser isolation this app started with, so what matters is that the parts
which must still hold — attribution, who may destroy a thread, and what happens
when two people type at once — actually do.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="needs a throwaway Postgres"
)

import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.session import SessionMaker, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import agent_loop, llm, repository as repo  # noqa: E402
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
    monkeypatch.setattr(llm, "generate_title", lambda *a: _title())

    async def answers(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_token("Checked.")
        result = llm.StreamedTurn()
        result.content = "Checked."
        return result

    monkeypatch.setattr(llm, "stream_completion", answers)
    yield
    await engine.dispose()


async def _title() -> str:
    return "Test conversation"


async def analyst() -> httpx.AsyncClient:
    """A browser with its own cookie, which is the only identity here."""
    client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    await client.post("/api/session")
    return client


async def _ask(client: httpx.AsyncClient, conversation_id: str, text: str) -> int:
    async with client.stream(
        "POST",
        f"/api/conversations/{conversation_id}/messages",
        json={"message": text},
    ) as response:
        async for _ in response.aiter_bytes():
            pass
        return response.status_code


async def test_one_analysts_thread_appears_in_everyone_elses_sidebar(stack):
    alice, bob, carol = await analyst(), await analyst(), await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "who touched WIN-FIN-04?")

        for other in (bob, carol):
            sidebar = (await other.get("/api/conversations")).json()
            assert [c["id"] for c in sidebar] == [created]
    finally:
        for client in (alice, bob, carol):
            await client.aclose()


async def test_a_shared_thread_says_who_started_it(stack):
    alice, bob = await analyst(), await analyst()
    try:
        alice_label = (await alice.get("/api/session")).json()["label"]
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "check this host")

        row = (await bob.get("/api/conversations")).json()[0]
        assert row["author_label"] == alice_label

        detail = (await bob.get(f"/api/conversations/{created}")).json()
        asked = [m for m in detail["messages"] if m["role"] == "user"][0]
        assert asked["author_label"] == alice_label, "the question lost its asker"
    finally:
        await alice.aclose()
        await bob.aclose()


async def test_another_analyst_can_continue_the_investigation(stack):
    """The point of sharing: a handover mid-shift should not need a new thread."""
    alice, bob = await analyst(), await analyst()
    try:
        bob_label = (await bob.get("/api/session")).json()["label"]
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "start here")

        assert await _ask(bob, created, "and now the assets") == 200

        detail = (await alice.get(f"/api/conversations/{created}")).json()
        questions = [(m["content"], m["author_label"]) for m in detail["messages"] if m["role"] == "user"]
        assert questions[1] == ("and now the assets", bob_label)
    finally:
        await alice.aclose()
        await bob.aclose()


async def test_only_the_analyst_who_started_a_thread_can_archive_it(stack):
    """Reading and contributing are shared; removing is not. One mis-click
    should not take somebody's investigation out of nineteen sidebars."""
    alice, bob = await analyst(), await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "mine")

        assert (await bob.delete(f"/api/conversations/{created}")).status_code == 403
        assert (
            await bob.patch(f"/api/conversations/{created}", json={"title": "renamed"})
        ).status_code == 403

        assert (await alice.delete(f"/api/conversations/{created}")).status_code == 204
        assert (await bob.get("/api/conversations")).json() == []
    finally:
        await alice.aclose()
        await bob.aclose()


async def test_two_analysts_typing_at_once_get_an_explanation_not_a_500(stack, monkeypatch):
    """Both would compute the same next `seq` and one would lose the unique
    index. A 409 that says what happened is the honest outcome."""
    alice, bob = await analyst(), await analyst()
    started = asyncio.Event()
    release = asyncio.Event()

    async def stalls(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_token("working")
        started.set()
        await release.wait()
        result = llm.StreamedTurn()
        result.content = "working"
        return result

    monkeypatch.setattr(llm, "stream_completion", stalls)
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        slow = asyncio.create_task(_ask(alice, created, "long one"))
        await asyncio.wait_for(started.wait(), timeout=5)

        clash = await bob.post(
            f"/api/conversations/{created}/messages", json={"message": "me too"}
        )
        assert clash.status_code == 409
        assert "another analyst" in clash.json()["detail"].lower()

        release.set()
        await slow
    finally:
        release.set()
        await alice.aclose()
        await bob.aclose()


async def test_the_thread_frees_up_once_the_turn_finishes(stack):
    alice, bob = await analyst(), await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "first")
        # The guard is keyed off a live streaming row, so a settled turn must
        # not leave the thread locked for the rest of the day.
        assert await _ask(bob, created, "second") == 200
    finally:
        await alice.aclose()
        await bob.aclose()


async def test_sharing_can_be_turned_off(stack, monkeypatch):
    """It is a privacy trade, so it has to be reversible without a code change."""
    monkeypatch.setattr(settings, "shared_workspace", False)
    alice, bob = await analyst(), await analyst()
    try:
        created = (await alice.post("/api/conversations")).json()["id"]
        await _ask(alice, created, "private")

        assert (await bob.get("/api/conversations")).json() == []
        assert (await bob.get(f"/api/conversations/{created}")).status_code == 404
    finally:
        await alice.aclose()
        await bob.aclose()


async def test_a_new_thread_reaches_every_sidebar_live(stack):
    """The sidebar is driven by Postgres NOTIFY, not polling. A real round trip
    through the listener, because the interesting failure is a payload that a
    broadcast never routes to subscribers registered under other session ids."""
    from app.services.events import event_bus

    await event_bus.start()
    for _ in range(50):  # give the listener its connection
        if event_bus.connected:
            break
        await asyncio.sleep(0.05)
    assert event_bus.connected, "the event bus never connected"

    alice, bob = uuid.uuid4(), uuid.uuid4()
    mine, theirs = event_bus.subscribe(alice), event_bus.subscribe(bob)
    try:
        async with SessionMaker() as db:
            await event_bus.publish(db, None, {"type": "conversation_created"})
            await db.commit()

        # NOTIFY is delivered on COMMIT, then dispatched by asyncpg's reader.
        assert (await asyncio.wait_for(mine.get(), timeout=5))["type"] == (
            "conversation_created"
        )
        assert (await asyncio.wait_for(theirs.get(), timeout=5))["type"] == (
            "conversation_created"
        ), "the broadcast never reached the other analyst"
    finally:
        event_bus.unsubscribe(alice, mine)
        event_bus.unsubscribe(bob, theirs)
        await event_bus.aclose()


async def test_newest_conversation_is_first(stack):
    """Descending by last activity, so the thread someone just spoke in is the
    one at the top."""
    alice = await analyst()
    try:
        ids = []
        for prompt in ("oldest", "middle", "newest"):
            created = (await alice.post("/api/conversations")).json()["id"]
            await _ask(alice, created, prompt)
            ids.append(created)

        listed = [c["id"] for c in (await alice.get("/api/conversations")).json()]
        assert listed == list(reversed(ids))

        # And replying to the oldest lifts it back to the top.
        await _ask(alice, ids[0], "one more thing")
        listed = [c["id"] for c in (await alice.get("/api/conversations")).json()]
        assert listed[0] == ids[0]
    finally:
        await alice.aclose()
