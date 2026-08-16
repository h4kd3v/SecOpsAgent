from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.conversations import owned_conversation
from app.api.deps import current_session, message_limiter
from app.db.models import AnonSession, Conversation
from app.db.session import SessionMaker, get_db
from app.schemas import ApprovalRequest, SendMessageRequest
from app.services import repository as repo
from app.services.agent_loop import TurnContext, resume_turn, run_turn

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["chat"])

# Cleanup tasks for cancelled turns, kept alive against the GC.
_CLEANUP_TASKS: set[asyncio.Task] = set()

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # nginx buffers SSE into uselessness without this.
    "X-Accel-Buffering": "no",
}


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n"


async def _stream(
    request: Request, session_id: uuid.UUID, conversation_id: uuid.UUID, driver: str, payload: Any
) -> AsyncIterator[str]:
    """Runs the agent loop against its own DB session.

    The request-scoped session is not used here: a streaming response outlives
    normal dependency teardown, and any commit inside the loop must land even
    if the browser vanishes mid-turn.
    """
    async with SessionMaker() as db:
        session = await db.get(AnonSession, session_id)
        conversation = await db.get(Conversation, conversation_id)
        # Access was decided by the endpoint, which knows whether the workspace
        # is shared. Re-checking creator identity here would reject exactly the
        # case sharing exists for: a second analyst adding to someone's thread.
        if session is None or conversation is None:
            yield _sse({"type": "error", "message": "Conversation not found"})
            return

        ctx = TurnContext(db=db, conversation=conversation, session=session)
        generator = run_turn(ctx, payload) if driver == "message" else resume_turn(ctx, payload)

        interrupted = False
        detached = False
        try:
            async for event in generator:
                if await request.is_disconnected():
                    logger.info(
                        "client disconnected mid-turn; cancelling the turn",
                        extra={"conversation_id": str(conversation_id)},
                    )
                    interrupted = True
                    break
                yield _sse(event)
        except (asyncio.CancelledError, GeneratorExit):
            # Stop, or the tab went away. This scope is already being torn
            # down, so anything awaited here would itself be cancelled part
            # way through — hand the cleanup to a task that outlives us.
            interrupted = detached = True
            _finish_detached(generator, conversation_id)
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the UI
            logger.exception("agent loop crashed")
            interrupted = True
            detail = f"Internal error: {exc}"
            # Its own session: `db` may be mid-rollback after the crash, and an
            # error the analyst can still read tomorrow is the whole point.
            try:
                async with SessionMaker() as fresh:
                    await repo.record_error(fresh, conversation_id, detail)
            except Exception:  # noqa: BLE001
                logger.warning("could not record the crash on the transcript", exc_info=True)
            yield _sse({"type": "error", "message": detail})
        finally:
            if not detached and interrupted:
                # Closing the loop cancels the in-flight completion and every
                # outstanding MCP call; settling records what that left behind.
                with contextlib.suppress(Exception):
                    await generator.aclose()
                await _settle(conversation_id)

        # Deliberately outside the `finally`: yielding while a GeneratorExit is
        # in flight raises "async generator ignored GeneratorExit".
        yield "event: stream_end\ndata: {}\n\n"


async def _settle(conversation_id: uuid.UUID) -> None:
    """Repair an interrupted turn, always on a session of its own.

    Never the turn's session: a cancellation lands mid-flush often enough that
    it is left needing a rollback, and the repair is precisely the work that
    must not be lost to that.
    """
    try:
        async with SessionMaker() as db:
            await repo.settle_interrupted_turn(db, conversation_id)
    except Exception:  # noqa: BLE001
        logger.warning("failed to settle an interrupted turn", exc_info=True)


def _finish_detached(generator: AsyncIterator[dict[str, Any]], conversation_id: uuid.UUID) -> None:
    """Close the agent loop and repair the transcript, off the cancelled task."""

    async def finish() -> None:
        with contextlib.suppress(Exception):
            await generator.aclose()
        await _settle(conversation_id)

    task = asyncio.create_task(finish())
    # asyncio keeps only a weak reference to running tasks, so a bare
    # create_task can be garbage-collected before it finishes.
    _CLEANUP_TASKS.add(task)
    task.add_done_callback(_CLEANUP_TASKS.discard)


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    payload: SendMessageRequest,
    request: Request,
    session: AnonSession = Depends(current_session),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    await owned_conversation(conversation_id, session, db)
    message_limiter.check(str(session.id))
    # Shared threads mean two analysts can send into one conversation at the
    # same moment. Both would compute the same next `seq` and one would lose
    # the unique index; 409 with an explanation beats a 500.
    if await repo.has_active_turn(db, conversation_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Another analyst is already asking in this conversation. Wait for "
            "that answer to finish, or start a new chat.",
        )
    return StreamingResponse(
        _stream(request, session.id, conversation_id, "message", payload.message),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/{conversation_id}/approvals")
async def submit_approvals(
    conversation_id: uuid.UUID,
    payload: ApprovalRequest,
    request: Request,
    session: AnonSession = Depends(current_session),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    await owned_conversation(conversation_id, session, db)
    decisions = {d.invocation_id: d.decision for d in payload.decisions}
    return StreamingResponse(
        _stream(request, session.id, conversation_id, "approval", decisions),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
