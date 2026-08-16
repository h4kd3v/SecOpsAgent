"""Stopping a turn.

The analyst presses Stop; the browser aborts the fetch; Starlette cancels the
streaming generator. What matters is what is left behind: no work still running
server-side, and a transcript the *next* turn can build on. An orphaned
tool_call is the dangerous one — the completions API rejects that history
outright, so one abort would poison the conversation permanently.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="needs a throwaway Postgres"
)

from app.db.models import AnonSession, Base, Conversation, ToolInvocation  # noqa: E402
from app.db.session import SessionMaker, engine  # noqa: E402
from app.services import repository as repo  # noqa: E402


@pytest.fixture
async def db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with SessionMaker() as session:
        yield session
    await engine.dispose()


async def make_conversation(db) -> Conversation:
    session = AnonSession(label="Analyst test")
    db.add(session)
    await db.flush()
    conversation = Conversation(session_id=session.id)
    db.add(conversation)
    await db.commit()
    return conversation


def call(cid: str, name: str = "udm_search"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}


async def test_orphaned_tool_call_gets_a_reply(db):
    """Without this the next completion 400s on the whole conversation."""
    conversation = await make_conversation(db)
    await repo.add_message(db, conversation.id, "user", content="find it")
    await repo.add_message(
        db, conversation.id, "assistant", content=None, tool_calls=[call("c1")]
    )
    await db.commit()

    assert await repo.settle_interrupted_turn(db, conversation.id) is True

    history = await repo.load_history(db, conversation.id)
    replies = [m for m in history if m.role == "tool"]
    assert [m.tool_call_id for m in replies] == ["c1"]
    assert "cancelled" in (replies[0].content or "")

    # The whole point: the repaired transcript is one the API will accept.
    wire = repo.to_wire(history)
    ids = {c["id"] for m in wire if m.get("tool_calls") for c in m["tool_calls"]}
    assert ids <= {m["tool_call_id"] for m in wire if m["role"] == "tool"}


async def test_partial_answer_is_kept_not_discarded(db):
    """The analyst watched those tokens arrive; throwing them away on reload
    would look like data loss."""
    conversation = await make_conversation(db)
    message = await repo.add_message(
        db, conversation.id, "assistant", content="Twelve alerts fired", status="streaming"
    )
    await db.commit()

    await repo.settle_interrupted_turn(db, conversation.id)
    await db.refresh(message)

    assert message.status == "cancelled"
    assert message.content == "Twelve alerts fired"


async def test_running_invocations_are_closed_out(db):
    conversation = await make_conversation(db)
    message = await repo.add_message(
        db, conversation.id, "assistant", content=None, tool_calls=[call("c1")]
    )
    await db.flush()
    db.add(
        ToolInvocation(
            conversation_id=conversation.id,
            message_id=message.id,
            tool_call_id="c1",
            tool_name="udm_search",
            arguments={},
            status="running",
            is_write=False,
        )
    )
    await db.commit()

    await repo.settle_interrupted_turn(db, conversation.id)

    invocations = await repo.invocations_for(db, conversation.id)
    assert [i.status for i in invocations] == ["cancelled"]
    assert invocations[0].completed_at is not None


async def test_a_parked_write_is_left_alone(db):
    """An approval waiting on the analyst is a resting state, not damage. Its
    reply arrives when they decide; inventing a cancellation reply here would
    make the pending prompt unanswerable."""
    conversation = await make_conversation(db)
    message = await repo.add_message(
        db, conversation.id, "assistant", content=None, tool_calls=[call("c1", "close_case")]
    )
    await db.flush()
    db.add(
        ToolInvocation(
            conversation_id=conversation.id,
            message_id=message.id,
            tool_call_id="c1",
            tool_name="close_case",
            arguments={},
            status="pending_approval",
            is_write=True,
        )
    )
    await db.commit()

    await repo.settle_interrupted_turn(db, conversation.id)

    invocations = await repo.invocations_for(db, conversation.id)
    assert [i.status for i in invocations] == ["pending_approval"]
    history = await repo.load_history(db, conversation.id)
    assert not [m for m in history if m.role == "tool"]


async def test_settling_a_clean_conversation_changes_nothing(db):
    conversation = await make_conversation(db)
    await repo.add_message(db, conversation.id, "user", content="hello")
    await repo.add_message(db, conversation.id, "assistant", content="hi")
    await db.commit()

    assert await repo.settle_interrupted_turn(db, conversation.id) is False
    assert len(await repo.load_history(db, conversation.id)) == 2


async def test_empty_cancelled_turn_is_dropped_from_the_wire(db):
    """A cancelled turn that produced nothing must not become an empty
    assistant message; some gateways reject those."""
    conversation = await make_conversation(db)
    await repo.add_message(db, conversation.id, "user", content="hello")
    await repo.add_message(
        db, conversation.id, "assistant", content="", status="streaming"
    )
    await db.commit()

    await repo.settle_interrupted_turn(db, conversation.id)
    wire = repo.to_wire(await repo.load_history(db, conversation.id))

    assert [m["role"] for m in wire] == ["user"]


async def test_cancelling_the_caller_cancels_the_mcp_request(monkeypatch):
    """The actor must forward cancellation into the in-flight call rather than
    finishing a SecOps query nobody is waiting for."""
    from app.services import mcp_manager as module

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeSession:
        async def call_tool(self, name, arguments):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return None  # pragma: no cover - the sleep never completes

    actor = module._SessionActor()
    serve = asyncio.create_task(actor._serve(FakeSession()))

    caller = asyncio.create_task(actor.call("udm_search", {}))
    await asyncio.wait_for(started.wait(), timeout=2)

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    await asyncio.wait_for(cancelled.wait(), timeout=2)

    # The actor survives, so the next tool call does not pay for a reconnect.
    assert not serve.done()
    serve.cancel()


async def test_the_actor_still_serves_after_a_cancelled_call(monkeypatch):
    """Cancelling one call must not cost the session. The SDK sends
    notifications/cancelled and keeps the transport, so the next tool call
    should not have to pay for a reconnect."""
    from app.services import mcp_manager as module

    calls: list[str] = []

    class FakeSession:
        def __init__(self) -> None:
            self.first = True

        async def call_tool(self, name, arguments):
            calls.append(name)
            if self.first:
                self.first = False
                await asyncio.sleep(30)
            return "raw-result"

    monkeypatch.setattr(module, "_to_result", lambda raw, ms: raw)

    actor = module._SessionActor()
    serve = asyncio.create_task(actor._serve(FakeSession()))

    slow = asyncio.create_task(actor.call("slow_tool", {}))
    await asyncio.sleep(0.05)
    slow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await slow

    assert await asyncio.wait_for(actor.call("fast_tool", {}), timeout=2) == "raw-result"
    assert calls == ["slow_tool", "fast_tool"]
    serve.cancel()


class _NeverDisconnects:
    """Enough of starlette.Request for the streaming endpoint."""

    async def is_disconnected(self) -> bool:
        return False


async def test_closing_the_stream_repairs_the_transcript(db, monkeypatch):
    """The endpoint, closed mid-turn.

    Cancelling the in-flight pull is exactly what Starlette does to the
    response generator when the client goes away, so this drives the real
    cancellation path rather than calling the repair function directly.
    """
    from app.api import chat
    from app.services import agent_loop as loop_module
    from app.services import llm
    from app.services import tool_catalog as catalog_module
    from app.services.mcp_manager import ToolSpec

    conversation = await make_conversation(db)
    session_id = conversation.session_id

    class FakeMcp:
        last_error = None

        async def list_tools(self):
            return [ToolSpec("udm_search", "search", {"type": "object"}, read_only=True)]

    monkeypatch.setattr(loop_module, "mcp_manager", FakeMcp())
    monkeypatch.setattr(catalog_module, "mcp_manager", FakeMcp())
    monkeypatch.setattr(llm, "generate_title", lambda *a: _fake_title())

    stalled = asyncio.Event()

    async def stalls(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_token("Twelve alerts ")
        stalled.set()
        await asyncio.sleep(30)  # the analyst reaches for Stop about here

    monkeypatch.setattr(llm, "stream_completion", stalls)

    stream = chat._stream(
        _NeverDisconnects(), session_id, conversation.id, "message", "list alerts"
    )
    frames = await _pump(stream, until=stalled)
    assert any("token" in f for f in frames), "nothing streamed before the abort"

    # Starlette closes the response generator once the client is gone. Without
    # it a generator parked at an unconsumed `yield` never runs its cleanup.
    with contextlib.suppress(BaseException):
        await stream.aclose()
    await _drain_cleanup()

    async with SessionMaker() as fresh:
        history = await repo.load_history(fresh, conversation.id)

    assert not [m for m in history if m.status == "streaming"], (
        "an abandoned 'streaming' row blocks the next turn"
    )
    partial = [m for m in history if m.role == "assistant"]
    assert partial and partial[0].status == "cancelled"
    assert partial[0].content == "Twelve alerts ", "the streamed text should survive"


async def test_cancelling_settles_an_unanswered_tool_call(db, monkeypatch):
    """The failure that outlives the turn: a tool_call with no reply makes
    every later completion on this conversation 400."""
    from app.api import chat
    from app.services import agent_loop as loop_module
    from app.services import llm
    from app.services import tool_catalog as catalog_module
    from app.services.mcp_manager import ToolSpec

    conversation = await make_conversation(db)
    hung = asyncio.Event()

    class HangingMcp:
        last_error = None

        async def list_tools(self):
            return [ToolSpec("udm_search", "search", {"type": "object"}, read_only=True)]

        async def call_tool(self, name, arguments):
            hung.set()
            await asyncio.sleep(30)

    monkeypatch.setattr(loop_module, "mcp_manager", HangingMcp())
    monkeypatch.setattr(catalog_module, "mcp_manager", HangingMcp())
    monkeypatch.setattr(llm, "generate_title", lambda *a: _fake_title())

    async def asks_for_a_tool(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        result = llm.StreamedTurn()
        result.tool_calls = [call("c1")]
        return result

    monkeypatch.setattr(llm, "stream_completion", asks_for_a_tool)

    stream = chat._stream(
        _NeverDisconnects(), conversation.session_id, conversation.id, "message", "search"
    )
    await _pump(stream, until=hung)
    with contextlib.suppress(BaseException):
        await stream.aclose()
    await _drain_cleanup()

    async with SessionMaker() as fresh:
        history = await repo.load_history(fresh, conversation.id)

    wire = repo.to_wire(history)
    asked = {c["id"] for m in wire if m.get("tool_calls") for c in m["tool_calls"]}
    answered = {m["tool_call_id"] for m in wire if m["role"] == "tool"}
    assert asked, "the model should have asked for a tool"
    assert asked <= answered, "every tool_call needs a reply or the next turn 400s"


async def _fake_title() -> str:
    return "Test conversation"


async def _pump(stream, until: asyncio.Event, limit: float = 10.0) -> list[str]:
    """Pull frames until `until` fires, then cancel the pending pull.

    Cancelling an in-flight `__anext__` is precisely what Starlette does to a
    response generator when the client disconnects, so this drives the real
    path rather than a stand-in for it.
    """
    frames: list[str] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit

    while loop.time() < deadline:
        pull = asyncio.ensure_future(stream.__anext__())
        signal = asyncio.ensure_future(until.wait())
        done, _ = await asyncio.wait(
            {pull, signal}, return_when=asyncio.FIRST_COMPLETED
        )
        if pull in done:
            signal.cancel()
            try:
                frames.append(pull.result())
            except StopAsyncIteration:
                return frames
            continue

        # The turn is parked mid-flight and the analyst pressed Stop.
        pull.cancel()
        with contextlib.suppress(BaseException):
            await pull
        return frames

    raise AssertionError("the turn never reached the point we wanted to abort")


async def _drain_cleanup() -> None:
    """Let the endpoint's detached repair task finish.

    Not asserted on: it removes itself from the set the moment it completes,
    so a fast repair is indistinguishable from one that never ran. The DB
    assertions in each test are what actually prove it happened.
    """
    from app.api.chat import _CLEANUP_TASKS

    for _ in range(60):
        pending = list(_CLEANUP_TASKS)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            return
        await asyncio.sleep(0.02)


async def test_an_interrupted_turn_reopens_with_what_it_got(db):
    """What the analyst sees after refreshing mid-turn.

    The transcript endpoint has to carry the partial answer and say it was cut
    short — otherwise a refresh looks like the question was never asked.
    """
    import httpx
    from httpx import ASGITransport

    from app.main import app

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/session")
        conversation_id = (await client.post("/api/conversations")).json()["id"]

        async with SessionMaker() as setup:
            await repo.add_message(setup, uuid.UUID(conversation_id), "user", content="hi")
            await repo.add_message(
                setup,
                uuid.UUID(conversation_id),
                "assistant",
                content="Twelve alerts fired",
                status="streaming",
            )
            await setup.commit()
            await repo.settle_interrupted_turn(setup, uuid.UUID(conversation_id))

        detail = (await client.get(f"/api/conversations/{conversation_id}")).json()

    answer = [m for m in detail["messages"] if m["role"] == "assistant"][0]
    assert answer["content"] == "Twelve alerts fired"
    assert answer["status"] == "cancelled"
    # And the thread is findable again rather than stuck on the default.
    assert detail["conversation"]["title"] != "New conversation"


async def test_a_conversation_that_is_gone_is_reported_as_missing(db):
    """A URL restored after the thread was archived must 404, not 500: the UI
    falls back to a new chat on exactly this signal."""
    import httpx
    from httpx import ASGITransport

    from app.main import app

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/session")
        response = await client.get(f"/api/conversations/{uuid.uuid4()}")

    assert response.status_code == 404
