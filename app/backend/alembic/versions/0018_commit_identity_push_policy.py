"""Commit identities that belong to people, and a push policy per project.

Server-side commits were signed with a placeholder, BLPL at localhost:
written into each repository's config the first time a commit needed it, and
passed on every component commit. Those commits were pushed to real repositories under an
address that does not exist. The identity now comes from the person the commit
is made for — their identity for that project, else their account default —
and a commit with neither is refused rather than signed with a stand-in.

So: a default on the user, an override on the membership, and on the
membership too the outcome of that member's last push, because an automatic
push that fails must be recorded somewhere it can be shown. And on the project,
whether pushing is automatic. Null there is a project created before anyone
was asked, and behaves as manual, which is what every project did until now.

Nothing is backfilled. There is no truthful value to backfill with: the
placeholder is the thing being removed, and an account's email is not
necessarily the address its owner commits under. Repositories that still carry
the placeholder in their config have it removed when they are next opened —
that is a filesystem change, not a schema one, and a sealed repository cannot
be reached from here.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("git_name", sa.String(128), nullable=True))
    op.add_column("users", sa.Column("git_email", sa.String(254), nullable=True))
    op.add_column("project", sa.Column("push_policy", sa.String(16), nullable=True))
    op.add_column("project_member", sa.Column("git_name", sa.String(128), nullable=True))
    op.add_column("project_member", sa.Column("git_email", sa.String(254), nullable=True))
    op.add_column(
        "project_member",
        sa.Column("last_push_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("project_member", sa.Column("last_push_ok", sa.Boolean(), nullable=True))
    op.add_column(
        "project_member", sa.Column("last_push_error", sa.String(2000), nullable=True)
    )


def downgrade() -> None:
    for column in ("last_push_error", "last_push_ok", "last_push_at", "git_email", "git_name"):
        op.drop_column("project_member", column)
    op.drop_column("project", "push_policy")
    op.drop_column("users", "git_email")
    op.drop_column("users", "git_name")
