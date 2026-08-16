from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_session
from app.db.models import AnonSession, Conversation, ToolInvocation
from app.db.session import get_db
from app.schemas import (
    AppConfigOut,
    ToolCatalogOut,
    ConversationDetail,
    ConversationOut,
    InvocationOut,
    MessageOut,
    RenameRequest,
    ToolOut,
)
from app.config import get_settings
from app.services import repository as repo
from app.services.mcp_manager import McpUnavailable, mcp_manager
from app.services.tool_catalog import tool_catalog

router = APIRouter(prefix="/api", tags=["conversations"])
settings = get_settings()


@router.get("/config", response_model=AppConfigOut)
async def app_config(_: AnonSession = Depends(current_session)) -> AppConfigOut:
    return AppConfigOut(
        model_display_name=settings.llm_model_display_name,
        require_approval_for_write=settings.require_approval_for_write,
        demo_mode=settings.demo_mode,
    )


async def owned_conversation(
    conversation_id: uuid.UUID, session: AnonSession, db: AsyncSession
) -> Conversation:
    conversation = await db.get(Conversation, conversation_id)
    # 404 rather than 403 for another visitor's thread — don't confirm it exists.
    if conversation is None or conversation.session_id != session.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    session: AnonSession = Depends(current_session), db: AsyncSession = Depends(get_db)
) -> list[Conversation]:
    result = await db.execute(
        select(Conversation)
        .where(Conversation.session_id == session.id, Conversation.archived.is_(False))
        .order_by(Conversation.updated_at.desc())
        .limit(200)
    )
    return list(result.scalars().all())


@router.post("/conversations", response_model=ConversationOut, status_code=201)
async def create_conversation(
    session: AnonSession = Depends(current_session), db: AsyncSession = Depends(get_db)
) -> Conversation:
    conversation = Conversation(session_id=session.id)
    db.add(conversation)
    await db.flush()
    await repo.notify_conversation(db, conversation, "conversation_created")
    await db.commit()
    return conversation


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    session: AnonSession = Depends(current_session),
    db: AsyncSession = Depends(get_db),
) -> ConversationDetail:
    conversation = await owned_conversation(conversation_id, session, db)
    messages = await repo.load_history(db, conversation.id)
    invocations = await repo.invocations_for(db, conversation.id)
    return ConversationDetail(
        conversation=ConversationOut.model_validate(conversation, from_attributes=True),
        messages=[MessageOut.model_validate(m, from_attributes=True) for m in messages],
        invocations=[_invocation_out(i) for i in invocations],
        total_tokens=sum((m.token_usage or {}).get("total_tokens", 0) or 0 for m in messages),
    )


def _invocation_out(invocation: ToolInvocation) -> InvocationOut:
    preview, total = repo.invocation_preview(invocation)
    return InvocationOut(
        id=invocation.id,
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        arguments=invocation.arguments,
        status=invocation.status,
        is_write=invocation.is_write,
        latency_ms=invocation.latency_ms,
        error=invocation.error,
        result_preview=preview,
        result_chars=total,
    )


@router.get("/conversations/{conversation_id}/invocations/{invocation_id}/result")
async def invocation_result(
    conversation_id: uuid.UUID,
    invocation_id: uuid.UUID,
    session: AnonSession = Depends(current_session),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """The full text a tool returned, fetched only when an analyst opens it.

    Kept off the transcript because a single UDM page runs to hundreds of
    kilobytes and every turn ends in a reload of the whole thread.
    """
    await owned_conversation(conversation_id, session, db)
    invocation = await db.get(ToolInvocation, invocation_id)
    # Scoped to the conversation as well as the session: an id from another
    # thread must not resolve just because both belong to the same visitor.
    if invocation is None or invocation.conversation_id != conversation_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tool call not found")
    return {"id": str(invocation.id), "text": repo.result_text(invocation)}


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
async def rename_conversation(
    conversation_id: uuid.UUID,
    payload: RenameRequest,
    session: AnonSession = Depends(current_session),
    db: AsyncSession = Depends(get_db),
) -> Conversation:
    conversation = await owned_conversation(conversation_id, session, db)
    conversation.title = payload.title
    await db.flush()
    await repo.notify_conversation(db, conversation, "conversation_updated")
    await db.commit()
    return conversation


@router.delete("/conversations/{conversation_id}", status_code=204)
async def archive_conversation(
    conversation_id: uuid.UUID,
    session: AnonSession = Depends(current_session),
    db: AsyncSession = Depends(get_db),
) -> None:
    conversation = await owned_conversation(conversation_id, session, db)
    # Archive, never DELETE: the tool invocations under this thread are an
    # audit record and outlive the analyst's interest in the chat.
    conversation.archived = True
    await repo.audit(
        db,
        "conversation.archived",
        session_id=session.id,
        detail={"conversation_id": str(conversation.id)},
    )
    await repo.notify_conversation(db, conversation, "conversation_archived")
    await db.commit()


async def _catalog(db: AsyncSession, *, force: bool) -> ToolCatalogOut:
    try:
        catalog = await tool_catalog.get(db, force=force)
    except McpUnavailable as exc:
        # 503 with the root cause, not a 500 with a stack trace: this endpoint
        # is the first thing anyone checks when wiring up a new MCP server.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Could not reach the MCP server: {mcp_manager.last_error or exc}",
        ) from exc
    return ToolCatalogOut(
        tools=[
            ToolOut(name=t.name, description=t.description, read_only=t.read_only)
            for t in catalog.tools
        ],
        fetched_at=catalog.fetched_at,
        age_seconds=catalog.age_seconds,
        source=catalog.source,
        stale=catalog.stale,
        error=catalog.error,
        ttl_hours=settings.tool_cache_ttl_hours,
    )


@router.get("/tools", response_model=ToolCatalogOut)
async def list_tools(
    _: AnonSession = Depends(current_session), db: AsyncSession = Depends(get_db)
) -> ToolCatalogOut:
    """Served from the cached catalogue unless it is older than the TTL."""
    return await _catalog(db, force=False)


@router.post("/tools/refresh", response_model=ToolCatalogOut)
async def refresh_tools(
    session: AnonSession = Depends(current_session), db: AsyncSession = Depends(get_db)
) -> ToolCatalogOut:
    """Force a re-fetch, for when SecOps ships new tools inside the TTL."""
    result = await _catalog(db, force=True)
    await repo.audit(db, "tools.refreshed", session_id=session.id,
                     detail={"count": len(result.tools), "source": result.source})
    await db.commit()
    return result
