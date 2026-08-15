from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    pool_size=10,
    max_overflow=5,
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
