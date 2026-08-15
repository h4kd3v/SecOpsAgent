"""cache MCP tool definitions

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "mcp_tool_catalog",
        sa.Column("id", UUID, primary_key=True),
        # Unique: one cached catalogue per MCP server, so repointing
        # MCP_SERVER_URL cannot serve another server's tools.
        sa.Column("server_url", sa.String(500), nullable=False, unique=True),
        sa.Column("tools", postgresql.JSONB, nullable=False),
        sa.Column("tool_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fetched_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("mcp_tool_catalog")
