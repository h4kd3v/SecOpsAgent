"""Conversations exist to hold messages; an empty one is debris.

The UI no longer creates one until the first message is sent, so these cover
the sweep that clears rows left by the old behaviour or by a first turn that
died between create and send.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

pytestmark = pytest.mark.skipif(
    "TEST_DATABASE_URL" not in os.environ, reason="needs a throwaway Postgres"
)

from sqlalchemy import select, update  # noqa: E402

from app.db.models import AnonSession, Base, Conversation, utcnow  # noqa: E402
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


async def _session(db) -> AnonSession:
    visitor = AnonSession(label="Analyst test")
    db.add(visitor)
    await db.flush()
    return visitor


async def _age(db, conversation_id, hours: float) -> None:
    await db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(created_at=utcnow() - timedelta(hours=hours))
    )
    await db.commit()


async def test_old_empty_conversation_is_removed(db):
    visitor = await _session(db)
    empty = Conversation(session_id=visitor.id)
    db.add(empty)
    await db.commit()
    await _age(db, empty.id, 48)

    assert await repo.delete_empty_conversations(db, 24) == 1
    assert (await db.execute(select(Conversation))).scalars().all() == []


async def test_a_conversation_with_messages_is_never_touched(db):
    """The sweep must key on emptiness, not age — a year-old thread with a
    transcript is exactly what the audit trail is for."""
    visitor = await _session(db)
    used = Conversation(session_id=visitor.id)
    db.add(used)
    await db.flush()
    await repo.add_message(db, used.id, "user", content="check 1.2.3.4")
    await db.commit()
    await _age(db, used.id, 24 * 365)

    assert await repo.delete_empty_conversations(db, 24) == 0
    assert (await db.execute(select(Conversation))).scalar_one().id == used.id


async def test_a_recent_empty_conversation_survives(db):
    """A turn may be mid-flight: created seconds ago, first message not yet
    committed. The TTL is what keeps the sweep off it."""
    visitor = await _session(db)
    fresh = Conversation(session_id=visitor.id)
    db.add(fresh)
    await db.commit()

    assert await repo.delete_empty_conversations(db, 24) == 0
    assert (await db.execute(select(Conversation))).scalar_one().id == fresh.id


async def test_sweep_is_disabled_by_a_zero_ttl(db):
    visitor = await _session(db)
    empty = Conversation(session_id=visitor.id)
    db.add(empty)
    await db.commit()
    await _age(db, empty.id, 1000)

    assert await repo.delete_empty_conversations(db, 0) == 0
    assert len((await db.execute(select(Conversation))).scalars().all()) == 1


async def test_only_the_empty_ones_go(db):
    visitor = await _session(db)
    keep = Conversation(session_id=visitor.id)
    drop_a = Conversation(session_id=visitor.id)
    drop_b = Conversation(session_id=visitor.id)
    db.add_all([keep, drop_a, drop_b])
    await db.flush()
    await repo.add_message(db, keep.id, "user", content="real question")
    await db.commit()
    for conversation in (keep, drop_a, drop_b):
        await _age(db, conversation.id, 48)

    assert await repo.delete_empty_conversations(db, 24) == 2
    remaining = (await db.execute(select(Conversation))).scalars().all()
    assert [c.id for c in remaining] == [keep.id]
