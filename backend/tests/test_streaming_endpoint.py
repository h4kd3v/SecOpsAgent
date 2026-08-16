"""Full-stack streaming tests through the real ASGI app.

These exist because "only the first chunk of the response came back" is a bug
that unit tests miss completely: every layer works in isolation, and the loss
happens at a seam — the SSE frame encoding, the response generator, or the
client-side buffer. So these drive the actual endpoint over HTTP and assert on
the bytes.
"""

from __future__ import annotations

import json
import os

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
from app.services import agent_loop, llm  # noqa: E402
from app.services import repository as repo  # noqa: E402
from app.services.mcp_manager import ToolResult, ToolSpec  # noqa: E402

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


def call(cid: str, name: str, args: str = "{}"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


READ_TOOL = ToolSpec("search_udm", "search events", {"type": "object"}, read_only=True)


class FakeMcp:
    def __init__(self, text: str = "ok") -> None:
        self.text = text
        self.calls: list[tuple[str, dict]] = []
        self.last_error = None

    async def list_tools(self):
        return [READ_TOOL]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return ToolResult(ok=True, text=self.text, raw={"text": self.text}, latency_ms=1)


@pytest.fixture
async def wired(monkeypatch):
    """Real app, real database, fake LLM and MCP."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with SessionMaker() as db:
        session = AnonSession(label="Analyst test")
        db.add(session)
        await db.flush()
        conversation = Conversation(session_id=session.id)
        db.add(conversation)
        await db.commit()
        session_id, conversation_id = session.id, conversation.id

    async def override():
        async with SessionMaker() as db:
            return await db.get(AnonSession, session_id)

    app.dependency_overrides[current_session] = override
    monkeypatch.setattr(llm, "generate_title", lambda *a: _title())

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, conversation_id

    app.dependency_overrides.clear()
    await engine.dispose()


async def _title() -> str:
    return "Test conversation"


def scripted(chunks: list[str], tool_calls: list | None = None):
    """One completion that emits `chunks` verbatim, then optionally tool calls."""
    state = {"n": 0}

    async def fake_stream(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        state["n"] += 1
        result = llm.StreamedTurn()
        if state["n"] == 1 and tool_calls:
            result.tool_calls = tool_calls
            return result
        for chunk in chunks:
            await on_token(chunk)
        result.content = "".join(chunks)
        return result

    return fake_stream, state


async def collect(client, conversation_id, message="hello") -> list[dict]:
    events: list[dict] = []
    buffer = ""
    async with client.stream(
        "POST",
        f"/api/conversations/{conversation_id}/messages",
        json={"message": message},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async for raw in response.aiter_bytes():
            buffer += raw.decode()
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                for line in frame.split("\n"):
                    if line.startswith("data:"):
                        events.append(json.loads(line[5:].strip()))
    return events


def streamed_text(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e.get("type") == "token")


async def test_every_token_arrives_not_just_the_first(wired, monkeypatch):
    client, conversation_id = wired
    _use(monkeypatch, FakeMcp())

    chunks = [f"chunk-{i} " for i in range(500)]
    stream, _ = scripted(chunks)
    monkeypatch.setattr(llm, "stream_completion", stream)

    events = await collect(client, conversation_id)

    token_events = [e for e in events if e.get("type") == "token"]
    assert len(token_events) == 500, "tokens were dropped between loop and socket"
    assert streamed_text(events) == "".join(chunks)
    assert events[-1] == {}, "the stream_end sentinel is missing"


async def test_content_that_would_break_sse_framing_survives(wired, monkeypatch):
    """The classic truncation cause: a raw newline after `data:` ends the frame
    early, so everything past the model's first line vanishes. JSON-encoding
    each frame is what prevents it."""
    client, conversation_id = wired
    _use(monkeypatch, FakeMcp())

    hostile = [
        "line one\nline two\n\nline three",   # blank line = frame terminator
        "data: not-an-event\n",               # looks like an SSE field
        'quotes " and \\ backslashes',
        "unicode ✓ 日本語 🔐",
        "event: fake\ndata: {}\n\n",
    ]
    stream, _ = scripted(hostile)
    monkeypatch.setattr(llm, "stream_completion", stream)

    events = await collect(client, conversation_id)

    assert streamed_text(events) == "".join(hostile)


async def test_response_after_a_tool_call_also_streams_in_full(wired, monkeypatch):
    """The model decides to call MCP, reads the result, then answers. That
    second completion is the one earlier versions dropped."""
    client, conversation_id = wired
    fake = FakeMcp(text="42 events matched")
    _use(monkeypatch, fake)

    chunks = [f"word{i} " for i in range(200)]
    stream, _ = scripted(
        chunks,
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "search_udm", "arguments": '{"q": "x"}'},
            }
        ],
    )
    monkeypatch.setattr(llm, "stream_completion", stream)

    events = await collect(client, conversation_id)

    assert fake.calls == [("search_udm", {"q": "x"})]
    assert streamed_text(events) == "".join(chunks)
    assert [e.get("type") for e in events].count("message_start") == 2


async def test_full_tool_output_reaches_the_model(wired, monkeypatch):
    """The model decides what matters, so it gets the whole MCP response —
    not a clipped prefix it would silently reason from."""
    client, conversation_id = wired
    big = "row\n" * 50_000  # ~200 KB, far past any old hard-coded cap
    _use(monkeypatch, FakeMcp(text=big))

    seen: list[str] = []

    async def capture(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        result = llm.StreamedTurn()
        if not seen:
            seen.append("first")
            result.tool_calls = [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "search_udm", "arguments": "{}"},
                }
            ]
            return result
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        seen.append(tool_messages[0]["content"])
        await on_token("done")
        result.content = "done"
        return result

    monkeypatch.setattr(llm, "stream_completion", capture)

    await collect(client, conversation_id)

    delivered = seen[1]
    assert delivered == big, "the model was handed a truncated tool result"
    assert "TRUNCATED" not in delivered


async def test_hitting_the_output_token_limit_is_reported_as_such(wired, monkeypatch):
    """A cut-off answer must not be mistaken for a broken stream."""
    client, conversation_id = wired
    _use(monkeypatch, FakeMcp())

    async def truncated(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_token("This answer stops mid-")
        result = llm.StreamedTurn()
        result.content = "This answer stops mid-"
        result.finish_reason = "length"
        return result

    monkeypatch.setattr(llm, "stream_completion", truncated)

    events = await collect(client, conversation_id)

    warnings = [e for e in events if e.get("type") == "warning"]
    assert warnings, "a length-capped answer was reported as if it were complete"
    assert "token limit" in warnings[0]["message"]


async def test_transcript_matches_what_was_streamed(wired, monkeypatch):
    """Whatever the UI rendered live must equal what a page reload shows."""
    client, conversation_id = wired
    _use(monkeypatch, FakeMcp())

    chunks = [f"part{i}." for i in range(120)]
    stream, _ = scripted(chunks)
    monkeypatch.setattr(llm, "stream_completion", stream)

    events = await collect(client, conversation_id)

    detail = await client.get(f"/api/conversations/{conversation_id}")
    assistant = [m for m in detail.json()["messages"] if m["role"] == "assistant"]

    assert assistant[-1]["content"] == streamed_text(events)
    assert assistant[-1]["status"] == "complete"


async def test_usage_and_model_are_recorded_and_returned(wired, monkeypatch):
    """The gateway reports what it served and what it cost; both must survive
    into the stored message and back out of the API."""
    client, conversation_id = wired
    _use(monkeypatch, FakeMcp())

    async def with_usage(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_token("answer")
        result = llm.StreamedTurn()
        result.content = "answer"
        result.model = "anthropic/claude-opus-4-6-20260101"
        result.usage = {
            "prompt_tokens": 1200,
            "completion_tokens": 340,
            "total_tokens": 1540,
        }
        return result

    monkeypatch.setattr(llm, "stream_completion", with_usage)

    events = await collect(client, conversation_id)

    done = next(e for e in events if e.get("type") == "done")
    assert done["usage"]["total_tokens"] == 1540
    assert done["model"] == "anthropic/claude-opus-4-6-20260101"

    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
    assert assistant["model"] == "anthropic/claude-opus-4-6-20260101"
    assert assistant["token_usage"]["total_tokens"] == 1540
    assert detail["total_tokens"] == 1540


async def test_usage_is_estimated_when_the_gateway_omits_it(wired, monkeypatch):
    """Not every proxy honours stream_options.include_usage. Showing nothing
    looks like a bug, so fall back — but flag it as an estimate."""
    client, conversation_id = wired
    _use(monkeypatch, FakeMcp())

    async def no_usage(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_token("hello there")
        result = llm.StreamedTurn()
        result.content = "hello there"
        return result  # usage stays None

    monkeypatch.setattr(llm, "stream_completion", no_usage)

    await collect(client, conversation_id)

    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
    assert assistant["token_usage"]["estimated"] is True
    assert assistant["token_usage"]["total_tokens"] > 0


async def test_a_saved_conversation_reopens_with_everything_intact(wired, monkeypatch):
    """Clicking a thread in the sidebar must restore the transcript, the tool
    cards and the usage figures — not just the text."""
    client, conversation_id = wired
    fake = FakeMcp(text="4 events matched")
    _use(monkeypatch, fake)

    stream, _ = scripted(
        ["Findings ", "below."],
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "search_udm", "arguments": '{"q": "x"}'},
            }
        ],
    )
    monkeypatch.setattr(llm, "stream_completion", stream)
    await collect(client, conversation_id)

    # Simulate the analyst coming back later and clicking the sidebar entry.
    reopened = (await client.get(f"/api/conversations/{conversation_id}")).json()

    roles = [m["role"] for m in reopened["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert reopened["messages"][-1]["content"] == "Findings below."
    assert reopened["conversation"]["title"] == "Test conversation"

    invocation = reopened["invocations"][0]
    assert invocation["tool_name"] == "search_udm"
    assert invocation["status"] == "succeeded"
    # The transcript carries a bounded preview; the full text stays reachable.
    assert invocation["result_preview"] == "4 events matched"
    assert invocation["result_chars"] == len("4 events matched")
    full = await client.get(
        f"/api/conversations/{conversation_id}/invocations/{invocation['id']}/result"
    )
    assert full.json()["text"] == "4 events matched"
    assert reopened["total_tokens"] > 0


async def test_one_visitor_cannot_open_another_visitors_conversation(wired):
    """Sidebar history is scoped by session cookie; a guessed id must 404."""
    client, _ = wired
    import uuid as _uuid

    response = await client.get(f"/api/conversations/{_uuid.uuid4()}")
    assert response.status_code == 404


async def test_every_round_of_a_multi_tool_turn_reaches_the_client(wired, monkeypatch):
    """The model writes, calls a tool, writes again, calls another, then
    answers. Every one of those deltas has to arrive — not just the first
    round's, and not just the final answer."""
    client, conversation_id = wired
    fake = FakeMcp()
    _use(monkeypatch, fake)

    rounds = [
        {
            "tokens": ["Checking ", "the alert ", "table. "],
            "tool_calls": [call("c1", "search_udm", '{"q": "alerts"}')],
        },
        {
            "tokens": ["Twelve rows. ", "Now the assets. "],
            "tool_calls": [call("c2", "search_udm", '{"q": "assets"}')],
        },
        {"tokens": ["WIN-FIN-04 ", "is the common host."]},
    ]
    state = {"n": 0}

    async def multi_round(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        script = rounds[min(state["n"], len(rounds) - 1)]
        state["n"] += 1
        for chunk in script["tokens"]:
            await on_token(chunk)
        result = llm.StreamedTurn()
        result.content = "".join(script["tokens"])
        result.tool_calls = script.get("tool_calls", [])
        return result

    monkeypatch.setattr(llm, "stream_completion", multi_round)
    events = await collect(client, conversation_id, "which host is common?")

    streamed = "".join(e["text"] for e in events if e.get("type") == "token")
    expected = "".join(chunk for r in rounds for chunk in r["tokens"])
    assert streamed == expected, "text from a later round went missing"

    # And the tools ran in between, not all bunched at one end.
    order = [e.get("type") for e in events if e.get("type") in ("token", "tool_result")]
    assert order.count("tool_result") == 2
    assert order.index("tool_result") > 0, "a tool result arrived before any text"
    assert order[-1] == "token", "the final answer must come after the last tool"


async def test_reasoning_deltas_are_streamed_and_stored(wired, monkeypatch):
    """A gateway that exposes a reasoning channel should have it on screen as
    it arrives, not summarised after the fact."""
    client, conversation_id = wired
    _use(monkeypatch, FakeMcp())

    async def thinks_aloud(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        await on_reasoning("The alert count looks high. ")
        await on_token("Twelve alerts. ")
        await on_reasoning("Worth checking the host.")
        await on_token("All on WIN-FIN-04.")
        result = llm.StreamedTurn()
        result.content = "Twelve alerts. All on WIN-FIN-04."
        result.reasoning = "The alert count looks high. Worth checking the host."
        return result

    monkeypatch.setattr(llm, "stream_completion", thinks_aloud)
    events = await collect(client, conversation_id, "how many alerts?")

    kinds = [e.get("type") for e in events if e.get("type") in ("token", "reasoning")]
    # Interleaved exactly as the gateway produced them.
    assert kinds == ["reasoning", "token", "reasoning", "token"]

    async with SessionMaker() as db:
        history = await repo.load_history(db, conversation_id)
    answer = [m for m in history if m.role == "assistant"][-1]
    assert answer.reasoning == "The alert count looks high. Worth checking the host."

    # Working, not something the assistant said: it must not go back up.
    wire = repo.to_wire(history)
    assert not any("alert count looks high" in str(m) for m in wire)


async def test_the_query_streams_as_the_model_writes_it(wired, monkeypatch):
    """Tool arguments are model output too.

    They used to be accumulated in silence and revealed only once the round
    ended, which hides the most interesting part of an investigation: the
    question the model decided to ask SecOps.
    """
    client, conversation_id = wired
    _use(monkeypatch, FakeMcp())

    class Delta:
        def __init__(self, index, id=None, name=None, arguments=None):
            self.index = index
            self.id = id
            self.function = type("F", (), {"name": name, "arguments": arguments})()

    async def writes_a_query(messages, tools, on_token, on_reasoning=None, on_tool_delta=None):
        from app.services.llm import _ToolCallAccumulator

        accumulator = _ToolCallAccumulator()
        pieces = [
            Delta(0, id="c1", name="search_udm", arguments=""),
            Delta(0, arguments='{"query": "ip'),
            Delta(0, arguments='=1.2.3.4 AND'),
            Delta(0, arguments=' port=445"}'),
        ]
        for piece in pieces:
            for index in accumulator.add([piece]):
                await on_tool_delta(index, accumulator.snapshot(index))
        result = llm.StreamedTurn()
        result.tool_calls = accumulator.result()
        if any(m["role"] == "tool" for m in messages):
            result.tool_calls = []
            await on_token("Nothing on that port.")
            result.content = "Nothing on that port."
        return result

    monkeypatch.setattr(llm, "stream_completion", writes_a_query)
    events = await collect(client, conversation_id, "check 1.2.3.4")

    drafts = [e for e in events if e.get("type") == "tool_call_delta"]
    assert len(drafts) >= 4, "the query arrived in one lump, not as written"
    assert drafts[0]["name"] == "search_udm"
    # Each frame carries the query as far as it has been written.
    assert drafts[1]["arguments"] == '{"query": "ip'
    assert drafts[-1]["arguments"] == '{"query": "ip=1.2.3.4 AND port=445"}'

    # And the draft is superseded by a real, audited invocation.
    real = [e for e in events if e.get("type") == "tool_call"]
    assert real and real[0]["invocation"]["arguments"] == {
        "query": "ip=1.2.3.4 AND port=445"
    }
