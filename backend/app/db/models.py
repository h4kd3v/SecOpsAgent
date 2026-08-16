from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


TimestampCol = DateTime(timezone=True)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _created() -> Mapped[datetime]:
    """Timestamp with BOTH a Python and a server default.

    The server default is the source of truth for anything writing outside the
    ORM. The Python default matters just as much: without it, reading
    `created_at` on a freshly flushed row triggers a synchronous refresh from
    the database, which raises `MissingGreenlet` under asyncio.
    """
    return mapped_column(
        TimestampCol, nullable=False, default=utcnow, server_default=func.now()
    )

# Roles follow the OpenAI chat schema so rows map 1:1 onto the wire format.
MESSAGE_ROLES = ("system", "user", "assistant", "tool")
FEEDBACK_RATINGS = ("up", "down")
INVOCATION_STATUSES = (
    "pending_approval",
    "denied",
    "running",
    "succeeded",
    "failed",
    "timeout",
    # The analyst pressed Stop while this call was in flight. Distinct from
    # "failed": nothing went wrong, and the tool may well have run to
    # completion on the SecOps side before the cancellation landed.
    "cancelled",
)


class AnonSession(Base):
    """A browser, not a person. There is no registration: the first request
    mints one of these and drops a signed cookie, and every conversation hangs
    off it. Clearing cookies means losing the sidebar — the rows survive for
    audit, they just become unreachable from the UI."""

    __tablename__ = "anon_sessions"

    id: Mapped[uuid.UUID] = _pk()
    # Short human-readable handle so the audit trail isn't a wall of UUIDs.
    label: Mapped[str] = mapped_column(String(60), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created()
    last_seen_at: Mapped[datetime] = mapped_column(
        TimestampCol, nullable=False, default=utcnow, server_default=func.now()
    )

    __table_args__ = (Index("ix_anon_sessions_last_seen", "last_seen_at"),)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("anon_sessions.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="New conversation")
    archived: Mapped[bool] = mapped_column(default=False, nullable=False)
    # Running token totals for the thread, maintained as each turn completes.
    #
    # Derivable by summing messages.token_usage, but only by reading every
    # message of every conversation — which is exactly the query a cost report
    # across twenty analysts wants to run cheaply. Kept in the same transaction
    # as the message it counts, so the two cannot drift.
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # True once any turn in the thread had its usage approximated because the
    # gateway omitted it. Marks the total as a floor, not a measurement.
    usage_estimated: Mapped[bool] = mapped_column(nullable=False, default=False)
    # Money, accumulated the same way and for the same reason as the tokens.
    # Null-free: a thread whose model has no configured rate simply stays at 0.
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    # With twenty analysts sharing one sidebar the good investigations get
    # buried within a week. Pinned threads sort above everything else.
    pinned: Mapped[bool] = mapped_column(nullable=False, default=False)
    # Free-form labels. An incident or case number is just a tag by
    # convention ("INC-1234"), which keeps one mechanism instead of two.
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        TimestampCol,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_conversations_session_updated", "session_id", "updated_at"),
        # The sidebar's own ordering: pinned first, then most recent.
        Index("ix_conversations_pinned_updated", "pinned", "updated_at"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    # Who wrote this, when a thread is shared. Nullable: assistant and tool
    # rows have no author, and rows predating the shared workspace have none
    # either. SET NULL rather than CASCADE — losing a session must not delete
    # the messages, they are the record.
    author_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("anon_sessions.id", ondelete="SET NULL"), nullable=True
    )
    # Nullable on purpose: an assistant turn that is purely tool calls has no text.
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 'streaming' rows exist while tokens are still arriving, so a dropped
    # connection still leaves a partial transcript behind.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="complete")
    # The model's own analysis on the way to this answer, when the gateway
    # streams it. Shown to the analyst, never replayed to the model: it is
    # working, not something the assistant said.
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why a turn failed, kept beside the turn it failed on. A transient banner
    # is gone the moment the analyst switches conversations, which makes an
    # error the one part of the transcript they cannot go back and read.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # What this turn cost, at the rates in force when it ran. Null when the
    # model has no configured price — an absent number an operator can notice,
    # rather than a plausible one that is wrong.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created()

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    invocations: Mapped[list["ToolInvocation"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(f"role IN {MESSAGE_ROLES}", name="ck_messages_role"),
        Index("ix_messages_conversation_seq", "conversation_id", "seq", unique=True),
        # "what did this analyst ask?" across a shared workspace.
        Index("ix_messages_author", "author_session_id"),
    )


class ToolInvocation(Base):
    """Every MCP call, approved or not. This is the SOC audit trail."""

    __tablename__ = "tool_invocations"

    id: Mapped[uuid.UUID] = _pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    tool_call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    is_write: Mapped[bool] = mapped_column(nullable=False, default=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("anon_sessions.id", ondelete="SET NULL"), nullable=True
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _created()
    completed_at: Mapped[datetime | None] = mapped_column(TimestampCol, nullable=True)

    message: Mapped[Message] = relationship(back_populates="invocations")

    __table_args__ = (
        CheckConstraint(f"status IN {INVOCATION_STATUSES}", name="ck_invocations_status"),
        Index("ix_invocations_conversation", "conversation_id", "created_at"),
        Index("ix_invocations_tool_call_id", "tool_call_id"),
    )


class MessageFeedback(Base):
    """Whether an answer was any good, per analyst.

    One row per analyst per message rather than a counter on the message: in a
    shared workspace several people read the same answer, and "three analysts
    trusted this, one did not" is the signal worth having. It also makes a vote
    changeable without losing who cast it.
    """

    __tablename__ = "message_feedback"

    id: Mapped[uuid.UUID] = _pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("anon_sessions.id", ondelete="SET NULL"), nullable=True
    )
    rating: Mapped[str] = mapped_column(String(8), nullable=False)
    # Why. Optional, and the more useful half when it is filled in.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        CheckConstraint(f"rating IN {FEEDBACK_RATINGS}", name="ck_feedback_rating"),
        UniqueConstraint("message_id", "session_id", name="uq_feedback_message_session"),
        Index("ix_feedback_message", "message_id"),
    )


class McpToolCatalog(Base):
    """The MCP server's tool definitions, cached.

    Keyed by server URL so repointing MCP_SERVER_URL invalidates it rather
    than silently serving another server's tools.
    """

    __tablename__ = "mcp_tool_catalog"

    id: Mapped[uuid.UUID] = _pk()
    server_url: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    # Raw definitions including the server's own annotation hints, so
    # read/write classification is recomputed on read: changing
    # TOOL_READONLY_PATTERNS takes effect without a refetch.
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    tool_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_at: Mapped[datetime] = mapped_column(TimestampCol, nullable=False, default=utcnow)
    created_at: Mapped[datetime] = _created()


class AuditEvent(Base):
    """Security-relevant events outside the message flow."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = _pk()
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("anon_sessions.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (Index("ix_audit_events_created", "created_at"),)
