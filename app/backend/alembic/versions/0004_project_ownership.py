"""Projects get an owner and a member list.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-15

blpl.toml recorded a project's name, remote and branch — and nothing about whose
it was. That is why two different sign-ins landed in the same project and could
both have edited it.

The awkward part is the existing rows. The file records no owner, so there is
nothing to migrate *from*: any assignment here is a guess. The guess made is the
lowest user id, which on a single-operator install is right and on any other is
at least recoverable — the owner can share the project with whoever should have
had it, or hand it over. Leaving them unowned was the alternative and is worse:
an unowned project is one nobody can open, share, or delete, so the first
symptom would be every existing design disappearing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Unique because it is the directory name on disk. Two users cannot each
        # have a "baseboard" until working copies are namespaced per user, which
        # is the worktree work in the worker-pool phase.
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("remote", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("branch", sa.String(length=128), nullable=False, server_default="main"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("name", name="uq_project_name"),
    )

    op.create_table(
        "project_member",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_member"),
    )
    op.create_index("ix_project_member_user", "project_member", ["user_id"])

    _adopt_existing_projects()


def _adopt_existing_projects() -> None:
    """Give every project in blpl.toml an owner, so none is stranded.

    The owner row goes into project_member as well as project.owner_id, because
    every permission check asks one question — "is there a membership row" — and
    an owner who is only implied by a column is the case that gets forgotten.
    """
    import os
    from pathlib import Path

    conn = op.get_bind()
    first_user = conn.execute(sa.text("SELECT id FROM users ORDER BY id LIMIT 1")).scalar()
    if first_user is None:
        return  # nothing to adopt into

    config_path = Path(os.environ.get("BLPL_CONFIG", "/app/data/blpl.toml"))
    if not config_path.exists():
        return

    from app import appconfig

    for name, entry in appconfig.load(config_path).projects.items():
        conn.execute(
            sa.text(
                "INSERT INTO project (name, owner_id, remote, branch, created_at) "
                "VALUES (:n, :o, :r, :b, CURRENT_TIMESTAMP)"
            ),
            {"n": name, "o": first_user, "r": entry.remote, "b": entry.branch},
        )
        project_id = conn.execute(
            sa.text("SELECT id FROM project WHERE name = :n"), {"n": name}
        ).scalar()
        conn.execute(
            sa.text(
                "INSERT INTO project_member (project_id, user_id, role, created_at) "
                "VALUES (:p, :u, 'owner', CURRENT_TIMESTAMP)"
            ),
            {"p": project_id, "u": first_user},
        )


def downgrade() -> None:
    op.drop_index("ix_project_member_user", table_name="project_member")
    op.drop_table("project_member")
    op.drop_table("project")
