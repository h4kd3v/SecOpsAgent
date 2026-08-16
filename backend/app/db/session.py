from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

# A turn holds more than one connection at a time, and holds them for as long
# as the model takes to answer:
#
#   1. the request-scoped session from `get_db`, for auth and ownership;
#   2. a second session owned by the streaming response, held for the whole
#      turn — potentially minutes across several tool rounds;
#   3. a short-lived one per partial-answer checkpoint, roughly once a second.
#
# So the pool has to be sized against concurrent *turns*, not concurrent
# requests. At the default of 10+5 a twentieth analyst hitting send got a
# 30-second wait and then a QueuePool timeout, which surfaces as the whole
# turn failing. Postgres allows 100 connections by default, which is the real
# ceiling to stay under.
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=False,
)

SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Request-scoped session. Do NOT use inside a streaming response —
    the generator outlives ordinary dependency teardown; open your own
    session with `SessionMaker()` there instead."""
    async with SessionMaker() as session:
        yield session
