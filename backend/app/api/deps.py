from __future__ import annotations

import time
from collections import deque

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import COOKIE_NAME, read_session_token
from app.db.models import AnonSession
from app.db.session import get_db

settings = get_settings()


async def current_session(
    request: Request, db: AsyncSession = Depends(get_db)
) -> AnonSession:
    """Resolve the visitor from their signed cookie.

    Deliberately does not mint one: `POST /api/session` is the only place a
    session is created, so an expired cookie produces a clean 401 the frontend
    can act on rather than silently orphaning someone's chat history.
    """
    token = request.cookies.get(COOKIE_NAME)
    session_id = read_session_token(token) if token else None
    if session_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No session")

    session = await db.get(AnonSession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    return session


class RateLimiter:
    """Per-visitor sliding window. In-process, which is correct for a single
    backend container. Move to Redis before running multiple replicas."""

    def __init__(self, limit: int, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = deque()
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"Rate limit reached ({self.limit}/min). Wait a moment.",
            )
        hits.append(now)
        self._evict(now)

    def _evict(self, now: float) -> None:
        """Drop keys that have gone quiet.

        Sessions are anonymous and disposable — every cleared cookie mints a
        new one — so a dict keyed by session id and never pruned is a slow leak
        for the life of the process. Swept in bulk rather than per call so the
        common path stays O(1).
        """
        if len(self._hits) < 512:
            return
        stale = [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]
        for key in stale:
            self._hits.pop(key, None)


message_limiter = RateLimiter(settings.rate_limit_messages_per_minute)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "-")
