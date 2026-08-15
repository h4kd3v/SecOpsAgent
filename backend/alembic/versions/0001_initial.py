"""initial schema

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    # Anonymous visitors: no registration, no credentials. The signed cookie
    # holding this id is the only thing tying a browser to its history.
    op.create_table(
        "anon_sessions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("user_agent", sa.String(400), nullable=True),
        sa.Column("source_ip", sa.String(64), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_anon_sessions_last_seen", "anon_sessions", ["last_seen_at"])

    op.create_table(
        "conversations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "session_id",
            UUID,
            sa.ForeignKey("anon_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=False, server_default="New conversation"),
        sa.Column("archived", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_conversations_session_updated", "conversations", ["session_id", "updated_at"]
    )

    op.create_table(
        "messages",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        # nullable: an assistant turn may be tool calls only
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("tool_calls", postgresql.JSONB, nullable=True),
        sa.Column("tool_call_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="complete"),
        sa.Column("token_usage", postgresql.JSONB, nullable=True),
        sa.Column("model", sa.String(120), nullable=True),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "role IN ('system','user','assistant','tool')", name="ck_messages_role"
        ),
    )
    op.create_index(
        "ix_messages_conversation_seq", "messages", ["conversation_id", "seq"], unique=True
    )

    op.create_table(
        "tool_invocations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id", UUID, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("tool_call_id", sa.String(128), nullable=False),
        sa.Column("tool_name", sa.String(200), nullable=False),
        sa.Column("arguments", postgresql.JSONB, nullable=False),
        sa.Column("result", postgresql.JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("is_write", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "session_id",
            UUID,
            sa.ForeignKey("anon_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", TS, nullable=True),
        sa.CheckConstraint(
            "status IN ('pending_approval','denied','running','succeeded','failed','timeout')",
            name="ck_invocations_status",
        ),
    )
    op.create_index(
        "ix_invocations_conversation", "tool_invocations", ["conversation_id", "created_at"]
    )
    op.create_index("ix_invocations_tool_call_id", "tool_invocations", ["tool_call_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "session_id",
            UUID,
            sa.ForeignKey("anon_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("detail", postgresql.JSONB, nullable=True),
        sa.Column("source_ip", sa.String(64), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_events_created", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("tool_invocations")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("anon_sessions")
