"""Token totals per conversation.

Usage was already recorded per assistant message. The thread-level total is
what makes "what did this analyst cost last month?" a cheap query rather than a
scan of every message in the database — so the number has to be right, and it
has to stay right when turns fail, get stopped, or run several tool rounds.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="needs a throwaway Postgres"
)

from sqlalchemy import func, select  # noqa: E402

from app.db.models import AnonSession, Base, Conversation  # noqa: E402
from app.db.session import SessionMaker, engine  # noqa: E402
from app.services import agent_loop, llm, repository as repo  # noqa: E402
from app.services.agent_loop import TurnContext, run_turn  # noqa: E402
from app.services.mcp_manager import ToolResult, ToolSpec  # noqa: E402

READ_TOOL = ToolSpec("udm_search", "search", {"type": "object"}, read_only=True)


class FakeMcp:
    last_error = None

    async def list_tools(self):
        return [READ_TOOL]

    async def call_tool(self, name, arguments):
        return ToolResult(ok=True, text="4 rows", raw={"text": "4 rows"}, latency_ms=1)


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
    monkeypatch.setattr(llm, "generate_title", lambda *a: _title())


async def _title() -> str:
    return "Test conversation"


async def make_context(db) -> TurnContext:
    session = AnonSession(label="Analyst test")
    db.add(session)
    await db.flush()
    conversation = Conversation(session_id=session.id)
    db.add(conversation)
    await db.commit()
    return TurnContext(db=db, conversation=conversation, session=session)


def usage(prompt: int, completion: int, estimated: bool = False) -> dict:
    out = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if estimated:
        out["estimated"] = True
    return out


async def test_a_completed_turn_adds_to_the_thread_total(db, wired, monkeypatch):
    async def answers(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_token("Nothing found.")
        result = llm.StreamedTurn()
        result.content = "Nothing found."
        result.usage = usage(1800, 40)
        return result

    monkeypatch.setattr(llm, "stream_completion", answers)

    ctx = await make_context(db)
    [e async for e in run_turn(ctx, "check 1.2.3.4")]

    async with SessionMaker() as fresh:
        conversation = await fresh.get(Conversation, ctx.conversation.id)

    assert conversation.prompt_tokens == 1800
    assert conversation.completion_tokens == 40
    assert conversation.total_tokens == 1840
    assert conversation.usage_estimated is False


async def test_every_round_of_a_tool_turn_is_counted(db, wired, monkeypatch):
    """Each round is a separate billed completion, and the prompt is re-sent
    every time — so the thread total is the sum, not the last round."""
    rounds = {"n": 0}

    async def two_rounds(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        rounds["n"] += 1
        result = llm.StreamedTurn()
        if rounds["n"] == 1:
            result.tool_calls = [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "udm_search", "arguments": "{}"},
                }
            ]
            result.usage = usage(1000, 20)
            return result
        await on_token("Done.")
        result.content = "Done."
        result.usage = usage(1500, 10)
        return result

    monkeypatch.setattr(llm, "stream_completion", two_rounds)

    ctx = await make_context(db)
    [e async for e in run_turn(ctx, "search")]

    async with SessionMaker() as fresh:
        conversation = await fresh.get(Conversation, ctx.conversation.id)

    assert conversation.total_tokens == 1020 + 1510


async def test_the_stored_total_matches_the_messages_it_sums(db, wired, monkeypatch):
    """The rollup is a denormalisation; if it can drift from the rows it
    summarises it is worse than not having it."""
    calls = {"n": 0}

    async def answers(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        calls["n"] += 1
        await on_token("ok")
        result = llm.StreamedTurn()
        result.content = "ok"
        result.usage = usage(100 * calls["n"], 5)
        return result

    monkeypatch.setattr(llm, "stream_completion", answers)

    ctx = await make_context(db)
    for prompt in ("one", "two", "three"):
        [e async for e in run_turn(ctx, prompt)]

    async with SessionMaker() as fresh:
        conversation = await fresh.get(Conversation, ctx.conversation.id)
        history = await repo.load_history(fresh, ctx.conversation.id)

    from_rows = sum((m.token_usage or {}).get("total_tokens", 0) for m in history)
    assert conversation.total_tokens == from_rows
    assert from_rows == (100 + 5) + (200 + 5) + (300 + 5)


async def test_a_failed_turn_adds_nothing(db, wired, monkeypatch):
    """Nothing reached the model, so nothing was billed. A total that grows on
    failure would overstate every cost report."""

    async def explodes(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        raise RuntimeError("Error code: 429 - insufficient_quota")

    monkeypatch.setattr(llm, "stream_completion", explodes)

    ctx = await make_context(db)
    [e async for e in run_turn(ctx, "check this")]

    async with SessionMaker() as fresh:
        conversation = await fresh.get(Conversation, ctx.conversation.id)

    assert conversation.total_tokens == 0
    assert conversation.prompt_tokens == 0


async def test_an_estimated_turn_marks_the_whole_thread(db, wired, monkeypatch):
    """Some gateways omit usage on streamed responses and the backend
    approximates it. A total mixing measured and guessed numbers is a floor,
    and should say so rather than pass as exact."""

    async def no_usage(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_token("answer")
        result = llm.StreamedTurn()
        result.content = "answer"
        result.usage = None  # gateway omitted it
        return result

    monkeypatch.setattr(llm, "stream_completion", no_usage)

    ctx = await make_context(db)
    [e async for e in run_turn(ctx, "hello")]

    async with SessionMaker() as fresh:
        conversation = await fresh.get(Conversation, ctx.conversation.id)

    assert conversation.total_tokens > 0
    assert conversation.usage_estimated is True


async def test_usage_per_analyst_is_one_query(db, wired, monkeypatch):
    """The reason the rollup exists: cost per analyst without touching the
    messages table at all."""

    async def answers(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_token("ok")
        result = llm.StreamedTurn()
        result.content = "ok"
        result.usage = usage(500, 25)
        return result

    monkeypatch.setattr(llm, "stream_completion", answers)

    ctx = await make_context(db)
    [e async for e in run_turn(ctx, "one")]
    [e async for e in run_turn(ctx, "two")]

    async with SessionMaker() as fresh:
        rows = (
            await fresh.execute(
                select(AnonSession.label, func.sum(Conversation.total_tokens))
                .join(Conversation, Conversation.session_id == AnonSession.id)
                .group_by(AnonSession.label)
            )
        ).all()

    assert rows == [("Analyst test", 1050)]


def test_add_usage_ignores_a_turn_with_no_usage():
    conversation = Conversation(session_id=uuid.uuid4())
    conversation.prompt_tokens = conversation.completion_tokens = conversation.total_tokens = 0
    conversation.usage_estimated = False

    repo.add_usage(conversation, None)
    repo.add_usage(conversation, {})

    assert conversation.total_tokens == 0
