from __future__ import annotations

import uuid
from typing import Any

from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditEvent, Conversation, Message, ToolInvocation, utcnow
from app.services.events import event_bus


async def next_seq(db: AsyncSession, conversation_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(Message.seq), 0)).where(
            Message.conversation_id == conversation_id
        )
    )
    return int(result.scalar_one()) + 1


async def add_message(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    role: str,
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_call_id: str | None = None,
    status: str = "complete",
    token_usage: dict[str, Any] | None = None,
    model: str | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        status=status,
        token_usage=token_usage,
        model=model,
        seq=await next_seq(db, conversation_id),
    )
    db.add(message)
    await db.flush()
    return message


async def load_history(db: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    result = await db.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    )
    return list(result.scalars().all())


def to_wire(messages: list[Message]) -> list[dict[str, Any]]:
    """DB rows -> OpenAI chat messages."""
    wire: list[dict[str, Any]] = []
    for m in messages:
        if m.status == "error":
            continue
        if m.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
            if m.tool_calls:
                entry["tool_calls"] = m.tool_calls
                # The API rejects empty-string content alongside tool_calls on
                # some proxies; None is the portable choice.
                entry["content"] = m.content or None
            wire.append(entry)
        elif m.role == "tool":
            wire.append(
                {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or ""}
            )
        else:
            wire.append({"role": m.role, "content": m.content or ""})
    return wire


async def touch_conversation(
    db: AsyncSession, conversation: Conversation, *, notify: bool = True
) -> None:
    # Set the attribute rather than issuing a Core UPDATE: a Core statement
    # would leave the in-memory object holding a stale `updated_at`, which is
    # the value the sidebar sorts on.
    conversation.updated_at = utcnow()
    await db.flush()
    if notify:
        await notify_conversation(db, conversation, "conversation_updated")


async def notify_conversation(
    db: AsyncSession, conversation: Conversation, event_type: str
) -> None:
    """Push a sidebar update to every tab this visitor has open."""
    await event_bus.publish(
        db,
        conversation.session_id,
        {
            "type": event_type,
            "conversation": {
                "id": str(conversation.id),
                "title": conversation.title,
                "created_at": conversation.created_at,
                "updated_at": conversation.updated_at,
            },
        },
    )


async def pending_invocations(
    db: AsyncSession, conversation_id: uuid.UUID
) -> list[ToolInvocation]:
    result = await db.execute(
        select(ToolInvocation)
        .where(
            ToolInvocation.conversation_id == conversation_id,
            ToolInvocation.status == "pending_approval",
        )
        .order_by(ToolInvocation.created_at)
    )
    return list(result.scalars().all())


async def invocations_for(db: AsyncSession, conversation_id: uuid.UUID) -> list[ToolInvocation]:
    result = await db.execute(
        select(ToolInvocation)
        .where(ToolInvocation.conversation_id == conversation_id)
        .order_by(ToolInvocation.created_at)
    )
    return list(result.scalars().all())


async def delete_empty_conversations(db: AsyncSession, older_than_hours: float) -> int:
    """Remove conversations that never received a message.

    The UI creates a conversation only when the first message is sent, so an
    empty one means the create succeeded and the turn that should have
    followed did not. There is nothing in it to preserve.
    """
    if older_than_hours <= 0:
        return 0
    cutoff = utcnow() - timedelta(hours=older_than_hours)
    result = await db.execute(
        delete(Conversation)
        .where(
            Conversation.created_at < cutoff,
            ~select(Message.id)
            .where(Message.conversation_id == Conversation.id)
            .exists(),
        )
        .returning(Conversation.id)
    )
    removed = len(result.fetchall())
    await db.commit()
    return removed


async def audit(
    db: AsyncSession,
    action: str,
    *,
    session_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
    source_ip: str | None = None,
) -> None:
    db.add(
        AuditEvent(session_id=session_id, action=action, detail=detail, source_ip=source_ip)
    )
