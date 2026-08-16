"""running token totals per conversation

Usage was recorded per assistant message and summed on demand. That answers
"what did this thread cost?" but not "what did this analyst cost last month?"
without reading every message row in the database. The totals now live on the
conversation, written in the same transaction as the message they count.

Existing threads are backfilled from the messages already stored, so the
column is correct for history rather than only for new work.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

COLUMNS = ("prompt_tokens", "completion_tokens", "total_tokens")


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column(
            "conversations",
            sa.Column(column, sa.Integer(), nullable=False, server_default="0"),
        )
    op.add_column(
        "conversations",
        sa.Column("usage_estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Backfill. `->>` yields text, so cast; COALESCE covers turns that failed
    # before the model ran and therefore have no usage at all.
    op.execute(
        """
        UPDATE conversations c SET
            prompt_tokens = COALESCE(t.prompt, 0),
            completion_tokens = COALESCE(t.completion, 0),
            total_tokens = COALESCE(t.total, 0),
            usage_estimated = COALESCE(t.estimated, false)
        FROM (
            SELECT conversation_id,
                   SUM(COALESCE((token_usage->>'prompt_tokens')::bigint, 0))     AS prompt,
                   SUM(COALESCE((token_usage->>'completion_tokens')::bigint, 0)) AS completion,
                   SUM(COALESCE((token_usage->>'total_tokens')::bigint, 0))      AS total,
                   bool_or(COALESCE((token_usage->>'estimated')::boolean, false)) AS estimated
            FROM messages
            WHERE token_usage IS NOT NULL
            GROUP BY conversation_id
        ) t
        WHERE t.conversation_id = c.id
        """
    )


def downgrade() -> None:
    op.drop_column("conversations", "usage_estimated")
    for column in reversed(COLUMNS):
        op.drop_column("conversations", column)
