"""keep the reason a turn failed

Errors were streamed to the browser and then dropped. Reopening the
conversation showed a gap where the answer should be, with nothing to say what
had happened — the one part of the transcript an analyst could not go back and
read. The text now lives on the message it belongs to.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "error")
