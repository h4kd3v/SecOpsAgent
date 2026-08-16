"""Regressions found by auditing the codebase.

Each of these is a defect that was reachable in normal use, not a hypothetical.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="needs a throwaway Postgres"
)

import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402

from app.db.models import Base, ToolInvocation  # noqa: E402
from app.db.session import SessionMaker, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services import repository as repo  # noqa: E402


@pytest.fixture
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/session")
        yield c
    await engine.dispose()


async def _thread_with_a_big_result(client, size: int = 400_000) -> tuple[str, str, str]:
    conversation_id = (await client.post("/api/conversations")).json()["id"]
    payload = "x" * size
    async with SessionMaker() as db:
        message = await repo.add_message(
            db,
            uuid.UUID(conversation_id),
            "assistant",
            content=None,
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "udm_search"}}],
        )
        await db.flush()
        invocation = ToolInvocation(
            conversation_id=uuid.UUID(conversation_id),
            message_id=message.id,
            tool_call_id="c1",
            tool_name="udm_search",
            arguments={"query": "ip=1.2.3.4"},
            result={"is_error": False, "text": payload, "structured_content": None},
            status="succeeded",
            is_write=False,
        )
        db.add(invocation)
        await db.commit()
        return conversation_id, str(invocation.id), payload


async def test_the_transcript_does_not_ship_the_whole_tool_result(client):
    """A UDM page is ~400 KB and every turn ends in a reload of the thread.
    Carrying results inline made that a multi-megabyte download of data the
    analyst had already seen."""
    conversation_id, _, payload = await _thread_with_a_big_result(client)

    response = await client.get(f"/api/conversations/{conversation_id}")
    body = response.content

    assert len(body) < 20_000, f"transcript shipped {len(body):,} bytes"
    invocation = response.json()["invocations"][0]
    assert invocation["result_chars"] == len(payload)
    assert len(invocation["result_preview"]) == repo.RESULT_PREVIEW_CHARS
    assert "result" not in invocation, "the raw payload is still being inlined"


async def test_the_full_result_is_still_reachable(client):
    """Bounding the transcript must not cost the audit trail."""
    conversation_id, invocation_id, payload = await _thread_with_a_big_result(client)

    response = await client.get(
        f"/api/conversations/{conversation_id}/invocations/{invocation_id}/result"
    )

    assert response.status_code == 200
    assert response.json()["text"] == payload


async def test_a_tool_result_from_another_thread_is_not_reachable(client):
    """Scoped to the conversation as well as the session: an id from another
    thread must not resolve just because both belong to the same visitor."""
    _, invocation_id, _ = await _thread_with_a_big_result(client)
    other = (await client.post("/api/conversations")).json()["id"]

    response = await client.get(
        f"/api/conversations/{other}/invocations/{invocation_id}/result"
    )

    assert response.status_code == 404


async def test_another_session_cannot_read_a_tool_result(client):
    conversation_id, invocation_id, _ = await _thread_with_a_big_result(client)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as intruder:
        await intruder.post("/api/session")
        response = await intruder.get(
            f"/api/conversations/{conversation_id}/invocations/{invocation_id}/result"
        )

    assert response.status_code == 404


def test_the_rate_limiter_does_not_grow_without_bound():
    """Sessions are anonymous and disposable — every cleared cookie mints a new
    one — so a dict keyed by session id and never pruned leaks for the life of
    the process."""
    from app.api.deps import RateLimiter

    limiter = RateLimiter(limit=100, window=0.0)
    for i in range(5_000):
        limiter.check(f"session-{i}")

    assert len(limiter._hits) < 1_000, f"{len(limiter._hits)} keys retained"


def test_the_rate_limiter_still_limits():
    from fastapi import HTTPException

    from app.api.deps import RateLimiter

    limiter = RateLimiter(limit=3)
    for _ in range(3):
        limiter.check("same-analyst")

    with pytest.raises(HTTPException) as caught:
        limiter.check("same-analyst")
    assert caught.value.status_code == 429


async def test_health_does_not_probe_upstreams_on_every_hit(client, monkeypatch):
    """The endpoint is unauthenticated by design — load balancers cannot hold a
    cookie — so uncached outbound probes let anyone who can reach it make the
    backend hammer SecOps and the LLM gateway at request rate."""
    from app.api import health
    from app.services import llm
    from app.services.mcp_manager import mcp_manager

    monkeypatch.setattr(health, "_probe_cache", None)
    calls = {"llm": 0, "mcp": 0}

    async def count_llm():
        calls["llm"] += 1
        return True

    async def count_mcp():
        calls["mcp"] += 1
        return True

    monkeypatch.setattr(llm, "ping", count_llm)
    monkeypatch.setattr(mcp_manager, "healthy", count_mcp)

    for _ in range(25):
        await client.get("/api/health/ready")

    assert calls["llm"] == 1, f"probed the gateway {calls['llm']} times"
    assert calls["mcp"] == 1


async def test_the_connection_budget_is_checked_at_startup(caplog, monkeypatch):
    """Each uvicorn worker opens its own pool. Sizing the pool per process and
    then running several workers quietly overcommits Postgres, and the failure
    lands mid-investigation as "sorry, too many clients already"."""
    import logging

    from app import main
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "db_pool_size", 40)
    monkeypatch.setattr(settings, "db_max_overflow", 20)
    monkeypatch.setenv("UVICORN_WORKERS", "2")

    with caplog.at_level(logging.ERROR):
        await main.check_connection_budget()

    assert any("connection budget exceeded" in r.message for r in caplog.records), (
        "overcommitting Postgres went unreported"
    )


async def test_a_budget_that_fits_is_not_reported_as_a_problem(caplog, monkeypatch):
    import logging

    from app import main
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "db_pool_size", 40)
    monkeypatch.setattr(settings, "db_max_overflow", 20)
    monkeypatch.setenv("UVICORN_WORKERS", "1")

    with caplog.at_level(logging.ERROR):
        await main.check_connection_budget()

    assert not [r for r in caplog.records if "exceeded" in r.message]
