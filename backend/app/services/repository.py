from __future__ import annotations

import uuid
from typing import Any

from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AnonSession, AuditEvent, Conversation, Message, ToolInvocation, utcnow
from app.services.events import event_bus

settings = get_settings()


# How much of a tool result travels with the transcript. Enough to see what
# came back; small enough that reopening a thread is not a multi-megabyte
# download. The full text is one request away, and the model always got it in
# full at the time.
RESULT_PREVIEW_CHARS = 2000

# How long a `streaming` row is taken to mean "a turn is running here". Long
# enough for a slow multi-round investigation, short enough that debris from a
# killed worker does not wedge a thread for the rest of the day.
ACTIVE_TURN_TIMEOUT_SECONDS = 300


def result_text(invocation: ToolInvocation) -> str:
    return (invocation.result or {}).get("text") or ""


def invocation_preview(invocation: ToolInvocation) -> tuple[str | None, int]:
    """(preview, total characters). Total lets the UI offer the rest."""
    text = result_text(invocation)
    if not text:
        return None, 0
    return text[:RESULT_PREVIEW_CHARS], len(text)


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
    author_session_id: uuid.UUID | None = None,
) -> Message:
    message = Message(
        conversation_id=conversation_id,
        role=role,
        author_session_id=author_session_id,
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
        if m.status == "cancelled" and not m.content and not m.tool_calls:
            # Stopped before the model produced anything. Partial text is kept
            # — it is real context — but an empty assistant turn is rejected
            # outright by some gateways.
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

    # A `tool` reply whose assistant turn was dropped above is just as fatal as
    # a tool_call with no reply — the API rejects both. Dropping the strays is
    # the only safe option once their turn is gone.
    asked = {
        call.get("id")
        for entry in wire
        for call in (entry.get("tool_calls") or [])
        if isinstance(call, dict)
    }
    return [m for m in wire if m["role"] != "tool" or m.get("tool_call_id") in asked]


def add_usage(conversation: Conversation, usage: dict[str, Any] | None) -> None:
    """Fold one turn's usage into the thread's running totals.

    Synchronous and in-memory on purpose: the caller commits it alongside the
    message it came from, so a total can never count a turn that rolled back.

    Failed turns carry no usage — nothing reached the model — and adding zero
    is the honest answer rather than skipping the row.
    """
    if not usage:
        return
    conversation.prompt_tokens += int(usage.get("prompt_tokens") or 0)
    conversation.completion_tokens += int(usage.get("completion_tokens") or 0)
    conversation.total_tokens += int(usage.get("total_tokens") or 0)
    if usage.get("estimated"):
        conversation.usage_estimated = True


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
    """Push a sidebar update.

    To everyone when the workspace is shared — a thread one analyst starts has
    to show up in the other nineteen sidebars, and each of them is subscribed
    under their own session id. Otherwise only to the tabs of the owner.
    """
    await event_bus.publish(
        db,
        None if settings.shared_workspace else conversation.session_id,
        {
            "type": event_type,
            "conversation": {
                "id": str(conversation.id),
                "title": conversation.title,
                "created_at": conversation.created_at,
                "updated_at": conversation.updated_at,
                "total_tokens": conversation.total_tokens,
                "usage_estimated": conversation.usage_estimated,
            },
        },
    )


async def has_active_turn(db: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Is a turn already running in this thread?

    Two analysts sending into one shared thread at the same moment would both
    compute the same next `seq` and one would lose the unique index — a 500
    where the honest answer is "someone else is asking, wait a moment".

    Keyed off the streaming row the turn already writes, so there is no second
    piece of state to keep in sync. A row left behind by a killed worker stops
    blocking once it ages out; the ordinary path clears it within milliseconds
    by settling the turn.
    """
    cutoff = utcnow() - timedelta(seconds=ACTIVE_TURN_TIMEOUT_SECONDS)
    result = await db.execute(
        select(Message.id)
        .where(
            Message.conversation_id == conversation_id,
            Message.status == "streaming",
            Message.created_at > cutoff,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def session_labels(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Author labels for a set of sessions, in one query."""
    if not ids:
        return {}
    result = await db.execute(
        select(AnonSession.id, AnonSession.label).where(AnonSession.id.in_(ids))
    )
    return {row[0]: row[1] for row in result.all()}


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


def title_from_prompt(text: str, limit: int = 60) -> str:
    """A title with no model behind it, for when the model is what failed."""
    first_line = " ".join(text.strip().split())
    if len(first_line) <= limit:
        return first_line or "New conversation"
    return first_line[:limit].rsplit(" ", 1)[0] + "…"


async def record_error(
    db: AsyncSession, conversation_id: uuid.UUID, text: str, *, model: str | None = None
) -> Message:
    """Append a failure to the transcript as a turn in its own right.

    For failures with no assistant row to hang off — the loop giving up, or a
    crash in the endpoint itself. Without this the analyst sees their prompt,
    no answer, and no reason.
    """
    message = await add_message(
        db, conversation_id, "assistant", content=None, status="error", model=model
    )
    message.error = text
    await db.commit()
    return message


CANCEL_NOTE = (
    "Tool call cancelled: the analyst stopped this turn before the tool returned. "
    "No result is available."
)


async def settle_interrupted_turn(db: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Bring a turn that was cut short back to a state the next one can build on.

    Three things are left dangling when a turn is aborted mid-flight:

    1. The assistant row is still `streaming`. Its partial text is real work —
       the analyst watched it arrive — so it is kept and marked `cancelled`
       rather than discarded.
    2. Invocations are still `running` even though their calls were cancelled.
    3. Most importantly, a `tool_call` may have no matching `tool` reply. The
       chat completions API rejects that transcript outright, so an aborted
       turn would poison every later turn in the conversation. Each orphan gets
       an explicit reply saying it was cancelled, which is also the honest
       thing to show the model.
    """
    changed = False

    result = await db.execute(
        update(Message)
        .where(Message.conversation_id == conversation_id, Message.status == "streaming")
        .values(status="cancelled")
        .returning(Message.id)
    )
    if result.fetchall():
        changed = True

    running = await db.execute(
        select(ToolInvocation).where(
            ToolInvocation.conversation_id == conversation_id,
            ToolInvocation.status == "running",
        )
    )
    for inv in running.scalars():
        inv.status = "cancelled"
        inv.completed_at = utcnow()
        inv.error = "cancelled by the analyst"
        changed = True

    history = await load_history(db, conversation_id)
    replied = {m.tool_call_id for m in history if m.role == "tool" and m.tool_call_id}
    # A parked write is a legitimate resting state, not damage: its reply
    # arrives when the analyst approves or declines it.
    parked = {
        inv.tool_call_id
        for inv in (await pending_invocations(db, conversation_id))
    }

    for message in history:
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call in message.tool_calls:
            call_id = call.get("id") if isinstance(call, dict) else None
            if not call_id or call_id in replied or call_id in parked:
                continue
            await add_message(
                db, conversation_id, "tool", tool_call_id=call_id, content=CANCEL_NOTE
            )
            replied.add(call_id)
            changed = True

    # A stopped turn never reaches the titling step, so without this every
    # thread an analyst interrupts stays "New conversation" — indistinguishable
    # from every other one in the sidebar. Deliberately not counted as a
    # repair: the return value answers "was this turn damaged?", and a missing
    # title is not damage.
    titled = False
    conversation = await db.get(Conversation, conversation_id)
    if conversation is not None and conversation.title == "New conversation":
        first_user = next((m.content for m in history if m.role == "user" and m.content), "")
        if first_user:
            conversation.title = title_from_prompt(first_user)
            await notify_conversation(db, conversation, "conversation_updated")
            titled = True

    if changed or titled:
        await db.commit()
    return changed


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
