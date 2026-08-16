"""What happened to a project, and when.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-15

The dashboard has to answer "what did I work on last", and nothing could. The
filesystem knows when a file changed but not who changed it or whether anyone
merely looked at it; git knows about commits but not about runs, imports or
shares. Neither can order a list the way someone thinks about their own work.

Nothing is backfilled. There is no record to backfill *from* — inventing
timestamps from file mtimes would produce a plausible history that never
happened, and a feed nobody can trust is worse than an empty one that fills up
as you work.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_activity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        # Nullable: some things happen to a project without a person doing them,
        # and attributing those to whoever triggered the request is a lie the
        # feed would then repeat back.
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("detail", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    # The dashboard asks two questions — "what did I touch last" and "what
    # happened here" — so both directions are indexed.
    op.create_index("ix_activity_user_time", "project_activity", ["user_id", "created_at"])
    op.create_index("ix_activity_project_time", "project_activity", ["project_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_activity_project_time", table_name="project_activity")
    op.drop_index("ix_activity_user_time", table_name="project_activity")
    op.drop_table("project_activity")
