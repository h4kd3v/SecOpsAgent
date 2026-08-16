"""allow the 'cancelled' invocation status

When an analyst presses Stop, in-flight tool calls are cancelled rather than
left to finish against SecOps. Those rows need a status of their own: "failed"
would be a lie — nothing went wrong — and leaving them "running" forever makes
the audit trail useless for answering "was this query actually sent?".

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

OLD = "('pending_approval', 'denied', 'running', 'succeeded', 'failed', 'timeout')"
NEW = (
    "('pending_approval', 'denied', 'running', 'succeeded', 'failed', 'timeout', "
    "'cancelled')"
)


def upgrade() -> None:
    op.drop_constraint("ck_invocations_status", "tool_invocations", type_="check")
    op.create_check_constraint("ck_invocations_status", "tool_invocations", f"status IN {NEW}")


def downgrade() -> None:
    # Rows the new status introduced would violate the old constraint, so they
    # are folded into the closest older meaning rather than blocking the
    # downgrade.
    op.execute("UPDATE tool_invocations SET status = 'failed' WHERE status = 'cancelled'")
    op.drop_constraint("ck_invocations_status", "tool_invocations", type_="check")
    op.create_check_constraint("ck_invocations_status", "tool_invocations", f"status IN {OLD}")
