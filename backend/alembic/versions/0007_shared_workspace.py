"""attribute messages to their author

With one shared workspace every analyst sees every thread, which makes "who
asked this?" a question the transcript has to answer. Assistant and tool rows
have no author; rows written before this migration have none either, and are
left null rather than guessed at.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("author_session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "messages_author_session_id_fkey",
        "messages",
        "anon_sessions",
        ["author_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Backfill from the thread's creator. Before the shared workspace a
    # conversation had exactly one participant, so this is accurate rather
    # than a guess.
    op.execute(
        """
        UPDATE messages m SET author_session_id = c.session_id
        FROM conversations c
        WHERE c.id = m.conversation_id AND m.role = 'user'
        """
    )
    op.create_index("ix_messages_author", "messages", ["author_session_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_author", table_name="messages")
    op.drop_constraint("messages_author_session_id_fkey", "messages", type_="foreignkey")
    op.drop_column("messages", "author_session_id")
