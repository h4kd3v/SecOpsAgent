"""Server-sent stream that keeps the sidebar live.

One long-lived connection per browser tab. Events originate from any backend
worker via Postgres NOTIFY, so a conversation created in one tab appears in the
other within milliseconds regardless of which uvicorn worker served it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.chat import SSE_HEADERS
from app.api.deps import current_session
from app.db.models import AnonSession
from app.services.events import event_bus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["events"])

# Long enough to be cheap, short enough to beat any proxy idle timeout.
HEARTBEAT_SECONDS = 25


@router.get("/events")
async def sidebar_events(
    request: Request, session: AnonSession = Depends(current_session)
) -> StreamingResponse:
    queue = event_bus.subscribe(session.id)

    async def generator() -> AsyncIterator[str]:
        # Tell the client to load the current list before streaming deltas, so
        # a reconnect after downtime can't leave a stale sidebar.
        yield 'event: resync\ndata: {"type": "resync"}\n\n'
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # Comment frame: keeps the connection warm, ignored by EventSource.
                    yield ": keepalive\n\n"
                    continue

                if await request.is_disconnected():
                    break
                yield f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, default=str)}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            event_bus.unsubscribe(session.id, queue)

    return StreamingResponse(generator(), media_type="text/event-stream", headers=SSE_HEADERS)
