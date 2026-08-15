from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.conversations import owned_conversation
from app.api.deps import current_session, message_limiter
from app.db.models import AnonSession, Conversation, Message
from app.db.session import SessionMaker, get_db
from app.schemas import ApprovalRequest, SendMessageRequest
from app.services.agent_loop import TurnContext, resume_turn, run_turn

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["chat"])

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
        if session is None or conversation is None or conversation.session_id != session_id:
            yield _sse({"type": "error", "message": "Conversation not found"})
            return

        ctx = TurnContext(db=db, conversation=conversation, session=session)
        generator = run_turn(ctx, payload) if driver == "message" else resume_turn(ctx, payload)

        try:
            async for event in generator:
                if await request.is_disconnected():
                    logger.info(
                        "client disconnected mid-turn; loop state is persisted",
                        extra={"conversation_id": str(conversation_id)},
                    )
                    break
                yield _sse(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the UI
            logger.exception("agent loop crashed")
            yield _sse({"type": "error", "message": f"Internal error: {exc}"})
        finally:
            # A disconnect leaves the in-flight assistant row marked
            # 'streaming'. Settle it so the next turn replays cleanly.
            try:
                await db.execute(
                    update(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.status == "streaming",
                    )
                    .values(status="complete")
                )
                await db.commit()
            except Exception:  # noqa: BLE001
                logger.warning("failed to settle streaming rows", exc_info=True)
            yield "event: stream_end\ndata: {}\n\n"


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
