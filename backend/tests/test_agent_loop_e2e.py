"""End-to-end agent-loop tests against a real Postgres, with the LLM and the
MCP server faked.

Run with:
    TEST_DATABASE_URL=postgresql+asyncpg://test:test@localhost:55433/test \
        pytest tests/test_agent_loop_e2e.py
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="needs a throwaway Postgres"
)

from app.db.models import AnonSession, Base, Conversation  # noqa: E402
from app.db.session import SessionMaker, engine  # noqa: E402
from app.services import agent_loop, llm  # noqa: E402
from app.services.agent_loop import TurnContext, resume_turn, run_turn  # noqa: E402
from app.services.mcp_manager import ToolResult, ToolSpec  # noqa: E402


class FakeMcp:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self.specs = specs
        self.calls: list[tuple[str, dict]] = []
        self.last_error = None

    async def list_tools(self):
        return self.specs

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return ToolResult(ok=True, text=f"{name} ok", raw={"text": "ok"}, latency_ms=5)


def scripted_llm(script):
    """Returns turns from `script` one per call, so the loop can be driven
    through tool rounds deterministically."""
    calls = {"n": 0}

    async def fake_stream(messages, tools, on_token, on_reasoning=None):
        turn = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        for piece in turn.get("tokens", []):
            await on_token(piece)
        result = llm.StreamedTurn()
        result.content = "".join(turn.get("tokens", []))
        result.tool_calls = turn.get("tool_calls", [])
        return result

    return fake_stream, calls


def call(cid: str, name: str, args: str = "{}"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


@pytest.fixture
async def db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with SessionMaker() as session:
        yield session
    # pytest-asyncio gives each test its own event loop; pooled connections
    # belong to the loop that opened them, so drop them between tests.
    await engine.dispose()


async def make_context(db) -> TurnContext:
    session = AnonSession(label="Analyst test")
    db.add(session)
    await db.flush()
    conversation = Conversation(session_id=session.id)
    db.add(conversation)
    await db.commit()
    return TurnContext(db=db, conversation=conversation, session=session)


def _use(monkeypatch, fake):
    """Point every binding site at the fake.

    `from x import y` binds per module: the agent loop calls `call_tool`
    directly, while tool definitions now come from the cached catalogue, which
    holds its own reference.
    """
    from app.services import agent_loop as loop_module
    from app.services import tool_catalog as catalog_module

    monkeypatch.setattr(loop_module, "mcp_manager", fake)
    monkeypatch.setattr(catalog_module, "mcp_manager", fake)
    return fake


READ_TOOL = ToolSpec("search_udm", "search events", {"type": "object"}, read_only=True)
WRITE_TOOL = ToolSpec("close_case", "close a case", {"type": "object"}, read_only=False)


async def test_read_tool_runs_without_approval(db, monkeypatch):
    fake = FakeMcp([READ_TOOL])
    _use(monkeypatch, fake)
    stream, _ = scripted_llm(
        [
            {"tool_calls": [call("c1", "search_udm", '{"query": "ip=1.2.3.4"}')]},
            {"tokens": ["No ", "matches."]},
        ]
    )
    monkeypatch.setattr(llm, "stream_completion", stream)
    monkeypatch.setattr(llm, "generate_title", lambda *a: _title())

    ctx = await make_context(db)
    events = [e async for e in run_turn(ctx, "Any traffic from 1.2.3.4?")]
    kinds = [e["type"] for e in events]

    assert fake.calls == [("search_udm", {"query": "ip=1.2.3.4"})]
    assert "approval_required" not in kinds
    assert kinds[-1] == "done"

    # The transcript must contain the tool response, or the next turn 400s.
    roles = [m.role for m in await _messages(db, ctx)]
    assert roles == ["user", "assistant", "tool", "assistant"]


async def test_write_tool_parks_until_approved(db, monkeypatch):
    fake = FakeMcp([READ_TOOL, WRITE_TOOL])
    _use(monkeypatch, fake)
    stream, calls = scripted_llm(
        [
            {"tool_calls": [call("c1", "close_case", '{"case_id": "7"}')]},
            {"tokens": ["Case closed."]},
        ]
    )
    monkeypatch.setattr(llm, "stream_completion", stream)
    monkeypatch.setattr(llm, "generate_title", lambda *a: _title())

    ctx = await make_context(db)
    events = [e async for e in run_turn(ctx, "Close case 7")]

    approval = [e for e in events if e["type"] == "approval_required"]
    assert approval, "a write tool must not execute unattended"
    assert fake.calls == [], "nothing may reach MCP before approval"
    assert calls["n"] == 1, "the loop must stop, not call the model again"

    invocation_id = approval[0]["invocations"][0]["id"]
    import uuid

    resumed = [
        e async for e in resume_turn(ctx, {uuid.UUID(invocation_id): "approve"})
    ]
    assert fake.calls == [("close_case", {"case_id": "7"})]
    assert resumed[-1]["type"] == "done"


async def test_denied_write_is_recorded_and_not_executed(db, monkeypatch):
    fake = FakeMcp([WRITE_TOOL])
    _use(monkeypatch, fake)
    stream, _ = scripted_llm(
        [
            {"tool_calls": [call("c1", "close_case", '{"case_id": "7"}')]},
            {"tokens": ["Understood, leaving it open."]},
        ]
    )
    monkeypatch.setattr(llm, "stream_completion", stream)
    monkeypatch.setattr(llm, "generate_title", lambda *a: _title())

    ctx = await make_context(db)
    events = [e async for e in run_turn(ctx, "Close case 7")]
    invocation_id = [e for e in events if e["type"] == "approval_required"][0][
        "invocations"
    ][0]["id"]

    import uuid

    [e async for e in resume_turn(ctx, {uuid.UUID(invocation_id): "deny"})]

    assert fake.calls == []
    from app.services import repository as repo

    invocations = await repo.invocations_for(db, ctx.conversation.id)
    assert [i.status for i in invocations] == ["denied"]


async def test_every_tool_is_offered_but_writes_still_gate(db, monkeypatch):
    """With anonymous access there is no role hierarchy, so the model sees the
    full toolset. The approval gate — not schema filtering — is what stops an
    unattended write."""
    fake = FakeMcp([READ_TOOL, WRITE_TOOL])
    _use(monkeypatch, fake)
    offered: list[list[str]] = []

    async def capture(messages, tools, on_token, on_reasoning=None):
        offered.append([t["function"]["name"] for t in tools])
        await on_token("done")
        result = llm.StreamedTurn()
        result.content = "done"
        return result

    monkeypatch.setattr(llm, "stream_completion", capture)
    monkeypatch.setattr(llm, "generate_title", lambda *a: _title())

    ctx = await make_context(db)
    [e async for e in run_turn(ctx, "hello")]

    assert offered == [["search_udm", "close_case"]]


async def test_sidebar_events_are_published_for_the_owning_session(db, monkeypatch):
    """The sidebar updates in real time only if the loop actually notifies."""
    fake = FakeMcp([READ_TOOL])
    _use(monkeypatch, fake)
    stream, _ = scripted_llm([{"tokens": ["Nothing found."]}])
    monkeypatch.setattr(llm, "stream_completion", stream)
    monkeypatch.setattr(llm, "generate_title", lambda *a: _title())

    published: list[tuple] = []

    async def capture(_db, session_id, event):
        published.append((session_id, event["type"]))

    from app.services import events as events_module

    monkeypatch.setattr(events_module.event_bus, "publish", capture)

    ctx = await make_context(db)
    [e async for e in run_turn(ctx, "check 1.2.3.4")]

    assert published, "no sidebar event was published"
    assert {sid for sid, _ in published} == {ctx.session.id}
    # The generated title has to reach the sidebar, or the thread stays
    # labelled "New conversation" until a manual refresh.
    assert "conversation_updated" in {kind for _, kind in published}


async def test_unknown_tool_is_answered_not_crashed(db, monkeypatch):
    fake = FakeMcp([READ_TOOL])
    _use(monkeypatch, fake)
    stream, _ = scripted_llm(
        [
            {"tool_calls": [call("c1", "hallucinated_tool", "{}")]},
            {"tokens": ["Sorry, I can't do that."]},
        ]
    )
    monkeypatch.setattr(llm, "stream_completion", stream)
    monkeypatch.setattr(llm, "generate_title", lambda *a: _title())

    ctx = await make_context(db)
    events = [e async for e in run_turn(ctx, "do something impossible")]

    assert fake.calls == []
    assert events[-1]["type"] == "done"
    # The model still gets a tool reply, so the conversation stays valid.
    assert any(m.role == "tool" for m in await _messages(db, ctx))


async def _title() -> str:
    return "Test conversation"


async def _messages(db, ctx):
    from app.services import repository as repo

    return await repo.load_history(db, ctx.conversation.id)
