"""Number automatic pushes, so the client can tell when its push has landed.

An automatic push runs after the response that started it has gone, and the
client's next status read showed the previous push's result: a failed
automatic push stayed invisible until a reload (Codex, PR #19). Each scheduled
push is now numbered on the member's row (push_requested), the number goes back
in the response, and the recorded result says the highest number it answers
for (last_push_seq). The client polls until it reaches its own.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_member",
        sa.Column("push_requested", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("project_member", sa.Column("last_push_seq", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("project_member", "last_push_seq")
    op.drop_column("project_member", "push_requested")
