"""Let one project override a task route, without a second table.

Task routing was per user and only per user, so "this project uses the cheap
model for stage1" had no way to be said. A project_id on the existing table
says it: NULL is the account-wide default, set is an override for one project.

Both scopes live in one table because they are the same thing read by the same
resolver. A separate table would have meant two shapes, two validations, and
two places for a fallback chain to be wrong.

The old unique constraint was (user_id, task), which a per-project row would
collide with immediately. It becomes (user_id, project_id, task) — and because
NULLs do not collide in a UNIQUE index, that alone would let a user accumulate
several account-wide rows for one task. A partial unique index covers the NULL
case explicitly.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_task_route",
        sa.Column("project_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_llm_task_route_project",
        "llm_task_route",
        "project",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("uq_llm_task_user_task", "llm_task_route", type_="unique")
    op.create_unique_constraint(
        "uq_llm_task_user_project_task",
        "llm_task_route",
        ["user_id", "project_id", "task"],
    )
    # NULLs do not collide in a UNIQUE index, so the constraint above does not
    # keep the account-wide default single. This does.
    op.create_index(
        "uq_llm_task_user_task_default",
        "llm_task_route",
        ["user_id", "task"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
    )
    op.create_index("ix_llm_task_route_project", "llm_task_route", ["project_id"])


def downgrade() -> None:
    # Overrides cannot be expressed without the column, so they go. Defaults —
    # the rows anyone actually configured first — survive.
    op.execute("DELETE FROM llm_task_route WHERE project_id IS NOT NULL")
    op.drop_index("ix_llm_task_route_project", table_name="llm_task_route")
    op.drop_index("uq_llm_task_user_task_default", table_name="llm_task_route")
    op.drop_constraint("uq_llm_task_user_project_task", "llm_task_route", type_="unique")
    op.drop_constraint("fk_llm_task_route_project", "llm_task_route", type_="foreignkey")
    op.drop_column("llm_task_route", "project_id")
    op.create_unique_constraint("uq_llm_task_user_task", "llm_task_route", ["user_id", "task"])
