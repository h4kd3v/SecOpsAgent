"""Real-time sidebar sync over Postgres LISTEN/NOTIFY.

The sidebar has to update the moment a conversation is created, retitled,
touched by a new message, or archived — including in the analyst's other tabs.

Postgres is already a required dependency and already has a pub/sub primitive,
so this needs no Redis and no queue. It also works correctly with more than one
uvicorn worker, which an in-process event bus would not: a NOTIFY reaches every
backend process holding a LISTEN, so whichever worker owns a given browser's
SSE connection still gets the event.

Payloads are tiny JSON documents (NOTIFY caps at 8000 bytes); anything larger
is fetched by the client over the normal REST API.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

CHANNEL = "secops_conversation_events"


def _dsn() -> str:
    """SQLAlchemy URL -> plain libpq DSN for the raw asyncpg listener."""
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue]] = {}
        self._connection: asyncpg.Connection | None = None
        self._task: asyncio.Task | None = None

    # -- publishing ---------------------------------------------------------

    async def publish(self, db: AsyncSession, session_id: uuid.UUID, event: dict[str, Any]) -> None:
        """Fire an event to every listener, in this process or another.

        Called inside the caller's transaction: the notification is delivered
        on COMMIT, so a client can never be told about a row that later rolls
        back.
        """
        payload = json.dumps({"session_id": str(session_id), **event}, default=str)
        if len(payload) > 7000:  # leave headroom under Postgres' 8000-byte cap
            payload = json.dumps(
                {"session_id": str(session_id), "type": event.get("type", "refresh")}
            )
        await db.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": CHANNEL, "payload": payload},
        )

    # -- subscribing --------------------------------------------------------

    def subscribe(self, session_id: uuid.UUID) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.setdefault(session_id, set()).add(queue)
        return queue

    def unsubscribe(self, session_id: uuid.UUID, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(session_id)
        if not queues:
            return
        queues.discard(queue)
        if not queues:
            self._subscribers.pop(session_id, None)

    def _dispatch(self, _conn, _pid, _channel, payload: str) -> None:
        try:
            event = json.loads(payload)
            session_id = uuid.UUID(event.pop("session_id"))
        except (ValueError, KeyError, TypeError):
            logger.warning("dropping malformed notify payload")
            return

        for queue in self._subscribers.get(session_id, set()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A client that can't keep up gets told to resync rather than
                # blocking the listener for everyone else.
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait({"type": "resync"})

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen_forever(), name="event-bus")

    async def _listen_forever(self) -> None:
        delay = 1.0
        while True:
            try:
                self._connection = await asyncpg.connect(_dsn())
                await self._connection.add_listener(CHANNEL, self._dispatch)
                logger.info("listening on %s", CHANNEL)
                delay = 1.0
                # Hold the connection open; asyncpg dispatches callbacks itself.
                while not self._connection.is_closed():
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("event bus connection lost, retrying", exc_info=True)
            finally:
                if self._connection is not None and not self._connection.is_closed():
                    with contextlib.suppress(Exception):
                        await self._connection.close()
                self._connection = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def aclose(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._connection is not None and not self._connection.is_closed():
            with contextlib.suppress(Exception):
                await self._connection.close()

    @property
    def connected(self) -> bool:
        return self._connection is not None and not self._connection.is_closed()


event_bus = EventBus()
