"""keep the model's analysis alongside the answer

Gateways that expose a reasoning stream send it as its own delta channel. It
was being dropped on the floor: the analyst saw a long pause and then an
answer, with no sight of the work that produced it. Stored separately from
`content` because it must never be replayed to the model as something the
assistant said.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("reasoning", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "reasoning")
