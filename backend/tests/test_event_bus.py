"""Real Postgres LISTEN/NOTIFY round trip.

This is what makes the sidebar live, and it's the component most likely to
break silently — a notification that never arrives just looks like a slightly
stale UI.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="needs a throwaway Postgres"
)

from app.db.session import SessionMaker, engine  # noqa: E402
from app.services.events import EventBus  # noqa: E402


@pytest.fixture
async def bus():
    instance = EventBus()
    await instance.start()
    # Give the listener connection a moment to attach before publishing.
    for _ in range(50):
        if instance.connected:
            break
        await asyncio.sleep(0.05)
    yield instance
    await instance.aclose()
    await engine.dispose()


async def _next(queue: asyncio.Queue, timeout: float = 5.0):
    return await asyncio.wait_for(queue.get(), timeout=timeout)


async def test_event_reaches_a_subscriber(bus):
    assert bus.connected, "listener never attached"

    session_id = uuid.uuid4()
    queue = bus.subscribe(session_id)

    async with SessionMaker() as db:
        await bus.publish(db, session_id, {"type": "conversation_created", "title": "hi"})
        await db.commit()  # NOTIFY is delivered on commit, not on execute

    event = await _next(queue)
    assert event["type"] == "conversation_created"
    assert event["title"] == "hi"


async def test_every_tab_of_one_session_gets_the_event(bus):
    session_id = uuid.uuid4()
    first = bus.subscribe(session_id)
    second = bus.subscribe(session_id)

    async with SessionMaker() as db:
        await bus.publish(db, session_id, {"type": "conversation_updated"})
        await db.commit()

    assert (await _next(first))["type"] == "conversation_updated"
    assert (await _next(second))["type"] == "conversation_updated"


async def test_events_do_not_leak_between_sessions(bus):
    """One visitor must never see another's conversations appear in their
    sidebar — the whole point of scoping by session id."""
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    my_queue = bus.subscribe(mine)

    async with SessionMaker() as db:
        await bus.publish(db, theirs, {"type": "conversation_created"})
        await bus.publish(db, mine, {"type": "conversation_updated"})
        await db.commit()

    # If isolation were broken, the first event out would be theirs.
    event = await _next(my_queue)
    assert event["type"] == "conversation_updated"
    assert my_queue.empty()


async def test_rolled_back_work_publishes_nothing(bus):
    """NOTIFY fires on COMMIT, so a client can never be told about a row that
    never existed."""
    session_id = uuid.uuid4()
    queue = bus.subscribe(session_id)

    async with SessionMaker() as db:
        await bus.publish(db, session_id, {"type": "conversation_created"})
        await db.rollback()

    with pytest.raises(asyncio.TimeoutError):
        await _next(queue, timeout=1.0)


async def test_unsubscribe_stops_delivery(bus):
    session_id = uuid.uuid4()
    queue = bus.subscribe(session_id)
    bus.unsubscribe(session_id, queue)

    async with SessionMaker() as db:
        await bus.publish(db, session_id, {"type": "conversation_updated"})
        await db.commit()

    with pytest.raises(asyncio.TimeoutError):
        await _next(queue, timeout=1.0)


async def test_oversized_payload_degrades_to_a_resync(bus):
    """Postgres caps NOTIFY at 8000 bytes; a huge title must not silently
    drop the event."""
    session_id = uuid.uuid4()
    queue = bus.subscribe(session_id)

    async with SessionMaker() as db:
        await bus.publish(
            db, session_id, {"type": "conversation_updated", "blob": "x" * 20_000}
        )
        await db.commit()

    event = await _next(queue)
    assert event["type"] == "conversation_updated"
    assert "blob" not in event  # trimmed; the client refetches over REST
