"""Twenty analysts at once, in their own sessions.

This is the failure nobody notices in testing and everybody notices in
production: one analyst sees another's investigation. Shared state in this app
is real — a single MCP session to Chronicle, one tool catalogue, one event bus —
so isolation is a property of the routing, not an accident of there being one
user. Everything here runs concurrently on purpose.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="needs a throwaway Postgres"
)

import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from app.api.deps import current_session  # noqa: E402
from app.db.models import AnonSession, Base, Conversation  # noqa: E402
from app.db.session import SessionMaker, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import agent_loop, llm, repository as repo  # noqa: E402
from app.services.mcp_manager import ToolResult, ToolSpec  # noqa: E402

ANALYSTS = 20
READ_TOOL = ToolSpec("udm_search", "search events", {"type": "object"}, read_only=True)


class SharedMcp:
    """One MCP session serving every analyst, as in production."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.last_error = None

    async def list_tools(self):
        return [READ_TOOL]

    async def call_tool(self, name, arguments):
        # Interleave deliberately: a shared actor must not pair a reply with
        # the wrong caller just because another request arrived mid-flight.
        await asyncio.sleep(0.02)
        self.calls.append(arguments)
        return ToolResult(
            ok=True,
            text=f"result for {arguments['q']}",
            raw={"text": arguments["q"]},
            latency_ms=1,
        )


@pytest.fixture
async def stack(monkeypatch):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    from app.services import tool_catalog as catalog_module

    shared = SharedMcp()
    monkeypatch.setattr(agent_loop, "mcp_manager", shared)
    monkeypatch.setattr(catalog_module, "mcp_manager", shared)

    # Every analyst asks about their own host and gets their own answer, so a
    # crossed wire shows up as the wrong string rather than a subtle mismatch.
    async def per_analyst(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        asked = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        host = asked.split()[-1]
        replies = [m for m in messages if m["role"] == "tool"]
        result = llm.StreamedTurn()
        if not replies:
            if on_reasoning:
                await on_reasoning(f"Looking up {host}. ")
            if on_tool_delta:
                await on_tool_delta(0, {"name": "udm_search", "arguments": f'{{"q": "{host}"'})
                await on_tool_delta(0, {"name": "udm_search", "arguments": f'{{"q": "{host}"}}'})
            result.tool_calls = [
                {
                    "id": f"call_{host}",
                    "type": "function",
                    "function": {"name": "udm_search", "arguments": json.dumps({"q": host})},
                }
            ]
            return result
        for piece in ("Findings for ", host, ": clean."):
            await on_token(piece)
        result.content = f"Findings for {host}: clean."
        return result

    monkeypatch.setattr(llm, "stream_completion", per_analyst)
    yield shared
    app.dependency_overrides.clear()
    await engine.dispose()


async def _title() -> str:
    return "Test conversation"


async def _make_analyst(label: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with SessionMaker() as db:
        session = AnonSession(label=label)
        db.add(session)
        await db.flush()
        conversation = Conversation(session_id=session.id)
        db.add(conversation)
        await db.commit()
        return session.id, conversation.id


async def _analyst_turn(host: str) -> tuple[str, str]:
    """A whole analyst, start to finish, over real cookies.

    Deliberately no dependency override: that is process-wide, so it could only
    ever test one identity at a time and would prove nothing about twenty at
    once. Identity here comes from the signed cookie, which is the mechanism
    production actually relies on.
    """
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/session")
        created = await client.post("/api/conversations")
        conversation_id = created.json()["id"]

        body = ""
        async with client.stream(
            "POST",
            f"/api/conversations/{conversation_id}/messages",
            json={"message": f"check host {host}"},
        ) as response:
            assert response.status_code == 200
            async for chunk in response.aiter_bytes():
                body += chunk.decode()
        return conversation_id, body


async def test_twenty_analysts_never_see_each_others_turns(stack):
    """The whole point: twenty turns genuinely in flight together, each about a
    different host. Every stream, and every stored transcript, must carry only
    its own."""
    hosts = [f"HOST-{i:02d}" for i in range(ANALYSTS)]
    results = await asyncio.gather(*(_analyst_turn(host) for host in hosts))

    for (conversation_id, body), host in zip(results, hosts):
        assert host in body, f"{host} did not receive its own answer"
        for other in hosts:
            if other != host:
                assert other not in body, f"{host}'s stream leaked {other}"

        async with SessionMaker() as db:
            history = await repo.load_history(db, uuid.UUID(conversation_id))
        text = " ".join(m.content or "" for m in history)
        assert host in text
        for other in hosts:
            if other != host:
                assert other not in text, f"{host}'s transcript contains {other}"


async def test_each_analyst_only_ever_lists_their_own_conversations(stack, monkeypatch):
    """With the shared workspace off, twenty sidebars built concurrently must
    not borrow rows from each other. (Sharing on is the opposite guarantee and
    is covered in test_shared_workspace.py.)"""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "shared_workspace", False)
    hosts = [f"HOST-{i:02d}" for i in range(ANALYSTS)]

    async def sidebar_for(host: str) -> list[str]:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/session")
            created = await client.post("/api/conversations")
            async with client.stream(
                "POST",
                f"/api/conversations/{created.json()['id']}/messages",
                json={"message": f"check host {host}"},
            ) as response:
                async for _ in response.aiter_bytes():
                    pass
            listed = await client.get("/api/conversations")
            return [c["id"] for c in listed.json()]

    sidebars = await asyncio.gather(*(sidebar_for(host) for host in hosts))

    for sidebar in sidebars:
        assert len(sidebar) == 1, "a sidebar showed more than this analyst created"
    flat = [cid for sidebar in sidebars for cid in sidebar]
    assert len(set(flat)) == len(flat), "two analysts were shown the same conversation"


async def test_a_conversation_is_invisible_to_another_session(stack, monkeypatch):
    """With sharing off, guessing a UUID must not be enough: ownership is
    enforced per request, on reads and on writes alike."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "shared_workspace", False)
    (mine, my_conversation) = await _make_analyst("Analyst A")
    (theirs, _) = await _make_analyst("Analyst B")

    async def as_b():
        async with SessionMaker() as db:
            return await db.get(AnonSession, theirs)

    app.dependency_overrides[current_session] = as_b
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        read = await client.get(f"/api/conversations/{my_conversation}")
        write = await client.post(
            f"/api/conversations/{my_conversation}/messages", json={"message": "hi"}
        )

    assert read.status_code == 404, "another session could read this thread"
    assert write.status_code == 404, "another session could post into this thread"


async def test_the_event_bus_fans_out_only_to_the_owning_session():
    """The sidebar stream is long-lived and per-browser; a mis-keyed dispatch
    would push one analyst's conversation titles into everyone's sidebar."""
    from app.services.events import event_bus

    mine, theirs = uuid.uuid4(), uuid.uuid4()
    my_queue = event_bus.subscribe(mine)
    their_queue = event_bus.subscribe(theirs)
    try:
        event_bus._dispatch(
            None,
            0,
            "chan",
            json.dumps({"session_id": str(mine), "type": "conversation_updated"}),
        )
        assert my_queue.get_nowait()["type"] == "conversation_updated"
        assert their_queue.empty(), "an event reached a session it was not addressed to"
    finally:
        event_bus.unsubscribe(mine, my_queue)
        event_bus.unsubscribe(theirs, their_queue)


async def test_the_shared_mcp_session_pairs_each_result_with_its_caller(stack):
    """One Chronicle session serves all twenty. Concurrent calls must not have
    their replies swapped."""
    from app.services.mcp_manager import _SessionActor

    class SlowSession:
        async def call_tool(self, name, arguments):
            # Later calls finish first, so a queue that assumed FIFO ordering
            # of replies would hand back the wrong one.
            await asyncio.sleep(0.05 - arguments["n"] * 0.004)
            return f"reply-{arguments['n']}"

    from app.services import mcp_manager as module

    actor = _SessionActor()
    serve = asyncio.create_task(actor._serve(SlowSession()))
    original = module._to_result
    module._to_result = lambda raw, ms: raw
    try:
        results = await asyncio.gather(
            *(actor.call("udm_search", {"n": i}) for i in range(10))
        )
    finally:
        module._to_result = original
        serve.cancel()

    assert results == [f"reply-{i}" for i in range(10)]
