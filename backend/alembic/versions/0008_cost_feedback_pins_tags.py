"""cost, feedback, pins, tags — and titles that say something

Five additions that share one migration because they land together:

* `cost_usd` on messages and conversations, so the question finance asks
  ("what did this cost?") is answerable without a spreadsheet of token rates.
* `message_feedback`, one row per analyst per answer — in a shared workspace
  several people read the same answer and their disagreement is the signal.
* `pinned` and `tags` on conversations, because twenty analysts sharing one
  sidebar bury the good investigations within a week.
* Titles backfilled from the first prompt. Every existing thread here is called
  "New conversation" because titling ran at the end of a turn and every one of
  those turns failed before reaching it.

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

TITLE_LIMIT = 60


def upgrade() -> None:
    op.add_column("messages", sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
    )
    op.add_column(
        "conversations",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "conversations",
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.create_index(
        "ix_conversations_pinned_updated", "conversations", ["pinned", "updated_at"]
    )

    op.create_table(
        "message_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("anon_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rating", sa.String(8), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("rating IN ('up', 'down')", name="ck_feedback_rating"),
        sa.UniqueConstraint("message_id", "session_id", name="uq_feedback_message_session"),
    )
    op.create_index("ix_feedback_message", "message_feedback", ["message_id"])

    # Titles from the first prompt. Collapsing whitespace matters: a pasted
    # multi-line query would otherwise become a sidebar row of newlines.
    op.execute(
        f"""
        UPDATE conversations c SET title = sub.title
        FROM (
            SELECT DISTINCT ON (m.conversation_id)
                   m.conversation_id,
                   CASE
                       WHEN length(trim(regexp_replace(m.content, '\\s+', ' ', 'g'))) > {TITLE_LIMIT}
                       THEN left(trim(regexp_replace(m.content, '\\s+', ' ', 'g')), {TITLE_LIMIT}) || '…'
                       ELSE trim(regexp_replace(m.content, '\\s+', ' ', 'g'))
                   END AS title
            FROM messages m
            WHERE m.role = 'user' AND m.content IS NOT NULL AND trim(m.content) <> ''
            ORDER BY m.conversation_id, m.seq
        ) sub
        WHERE sub.conversation_id = c.id
          AND c.title = 'New conversation'
          AND sub.title <> ''
        """
    )

    # Backfill cost where a rate can be inferred from what was already stored.
    # Nothing to do unless a turn recorded its rates, which only happens from
    # this release on, so this is a no-op today and correct on any re-run.
    op.execute(
        """
        UPDATE messages SET cost_usd =
            ROUND(
                (COALESCE((token_usage->>'prompt_tokens')::numeric, 0) / 1000000)
                    * (token_usage->>'input_rate_per_1m')::numeric
              + (COALESCE((token_usage->>'completion_tokens')::numeric, 0) / 1000000)
                    * (token_usage->>'output_rate_per_1m')::numeric,
                6)
        WHERE token_usage ? 'input_rate_per_1m'
        """
    )
    op.execute(
        """
        UPDATE conversations c SET cost_usd = COALESCE(t.total, 0)
        FROM (SELECT conversation_id, SUM(cost_usd) AS total
              FROM messages WHERE cost_usd IS NOT NULL GROUP BY conversation_id) t
        WHERE t.conversation_id = c.id
        """
    )


def downgrade() -> None:
    op.drop_index("ix_feedback_message", table_name="message_feedback")
    op.drop_table("message_feedback")
    op.drop_index("ix_conversations_pinned_updated", table_name="conversations")
    for column in ("tags", "pinned", "cost_usd"):
        op.drop_column("conversations", column)
    op.drop_column("messages", "cost_usd")
