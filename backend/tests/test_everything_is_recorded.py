"""Nothing goes in or out without landing in the database.

A transcript that quietly omits the turns that went wrong is worse than no
transcript: the analyst remembers asking, finds no trace, and cannot tell
whether the question was sent, whether the tool ran, or what came back. These
cover the paths where output was previously streamed to the browser and then
dropped.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="needs a throwaway Postgres"
)

from sqlalchemy import select  # noqa: E402

from app.db.models import AnonSession, AuditEvent, Base, Conversation  # noqa: E402
from app.db.session import SessionMaker, engine  # noqa: E402
from app.services import agent_loop, llm, repository as repo  # noqa: E402
from app.services.agent_loop import TurnContext, run_turn  # noqa: E402
from app.services.mcp_manager import ToolSpec  # noqa: E402

READ_TOOL = ToolSpec("udm_search", "search events", {"type": "object"}, read_only=True)


class FakeMcp:
    last_error = None

    async def list_tools(self):
        return [READ_TOOL]


@pytest.fixture
async def db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with SessionMaker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def wired(monkeypatch):
    from app.services import tool_catalog as catalog_module

    monkeypatch.setattr(agent_loop, "mcp_manager", FakeMcp())
    monkeypatch.setattr(catalog_module, "mcp_manager", FakeMcp())


async def make_context(db) -> TurnContext:
    session = AnonSession(label="Analyst test")
    db.add(session)
    await db.flush()
    conversation = Conversation(session_id=session.id)
    db.add(conversation)
    await db.commit()
    return TurnContext(db=db, conversation=conversation, session=session)


async def test_a_one_word_prompt_is_stored_before_the_model_runs(db, wired, monkeypatch):
    """Even when the model immediately fails, the question itself is on record."""

    async def explodes(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        raise RuntimeError("gateway on fire")

    monkeypatch.setattr(llm, "stream_completion", explodes)

    ctx = await make_context(db)
    [e async for e in run_turn(ctx, "why")]

    history = await repo.load_history(db, ctx.conversation.id)
    assert [m.content for m in history if m.role == "user"] == ["why"]


async def test_a_failed_turn_keeps_its_reason(db, wired, monkeypatch):
    async def explodes(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        raise RuntimeError("Error code: 429 - insufficient_quota")

    monkeypatch.setattr(llm, "stream_completion", explodes)

    ctx = await make_context(db)
    events = [e async for e in run_turn(ctx, "list alerts")]
    assert [e["type"] for e in events][-1] == "error"

    # Reopened later, from a session that never saw the stream.
    async with SessionMaker() as fresh:
        history = await repo.load_history(fresh, ctx.conversation.id)

    failed = [m for m in history if m.status == "error"]
    assert failed, "the failure vanished from the transcript"
    assert failed[0].error and "quota" in failed[0].error.lower()


async def test_the_raw_provider_error_reaches_the_audit_trail(db, wired, monkeypatch):
    """The analyst gets a sentence; the operator needs the payload."""

    async def explodes(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        raise RuntimeError("Error code: 429 - {'type': 'insufficient_quota'}")

    monkeypatch.setattr(llm, "stream_completion", explodes)

    ctx = await make_context(db)
    [e async for e in run_turn(ctx, "list alerts")]

    async with SessionMaker() as fresh:
        rows = (
            (await fresh.execute(select(AuditEvent).where(AuditEvent.action == "completion.failed")))
            .scalars()
            .all()
        )
    assert rows, "nothing was audited"
    assert "insufficient_quota" in rows[0].detail["raw"]


async def test_tokens_streamed_before_a_failure_are_kept(db, wired, monkeypatch):
    async def half_answers(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_token("Twelve alerts fired, ")
        raise RuntimeError("connection reset")

    monkeypatch.setattr(llm, "stream_completion", half_answers)

    ctx = await make_context(db)
    [e async for e in run_turn(ctx, "list alerts")]

    async with SessionMaker() as fresh:
        history = await repo.load_history(fresh, ctx.conversation.id)

    failed = [m for m in history if m.status == "error"][0]
    assert failed.content == "Twelve alerts fired, "
    assert failed.error, "the partial answer must not replace the reason"


async def test_a_failed_turn_is_still_findable_in_the_sidebar(db, wired, monkeypatch):
    """Titling asks the model for a summary — the very thing that just failed.
    A thread stuck on "New conversation" is one the analyst cannot find again."""

    async def explodes(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        raise RuntimeError("gateway on fire")

    async def never_call_me(*args, **kwargs):
        raise AssertionError("the model must not be asked to title a failed turn")

    monkeypatch.setattr(llm, "stream_completion", explodes)

    ctx = await make_context(db)
    [e async for e in run_turn(ctx, "why are these alerts firing on WIN-FIN-04")]

    async with SessionMaker() as fresh:
        conversation = await fresh.get(Conversation, ctx.conversation.id)

    assert conversation.title != "New conversation"
    assert conversation.title.startswith("why are these alerts")


async def test_an_errored_turn_is_not_replayed_to_the_model(db, wired, monkeypatch):
    """Stored for the analyst, hidden from the model: a failure is not
    something the assistant said."""
    ctx = await make_context(db)
    await repo.add_message(db, ctx.conversation.id, "user", content="list alerts")
    failed = await repo.add_message(
        db, ctx.conversation.id, "assistant", content=None, status="error"
    )
    failed.error = "The gateway is out of quota."
    await db.commit()

    wire = repo.to_wire(await repo.load_history(db, ctx.conversation.id))
    assert [m["role"] for m in wire] == ["user"]


async def test_a_stray_tool_reply_is_dropped_from_the_wire(db, wired):
    """Its assistant turn was filtered out, and a `tool` message with no
    preceding tool_call is rejected just as hard as the reverse."""
    ctx = await make_context(db)
    await repo.add_message(db, ctx.conversation.id, "user", content="search")
    failed = await repo.add_message(
        db,
        ctx.conversation.id,
        "assistant",
        content=None,
        status="error",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": "udm_search"}}],
    )
    failed.error = "died mid-turn"
    await repo.add_message(
        db, ctx.conversation.id, "tool", tool_call_id="c1", content="orphaned"
    )
    await db.commit()

    wire = repo.to_wire(await repo.load_history(db, ctx.conversation.id))
    assert [m["role"] for m in wire] == ["user"]
