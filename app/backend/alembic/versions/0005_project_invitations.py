"""Sharing becomes an invitation the recipient accepts.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-15

Adding someone to a project took effect the moment the owner clicked. Fine for a
row in a table; wrong for something that puts a project in your list, spends your
provider key on its runs, and — once project files are encrypted — hands you
material you become responsible for.

Existing memberships are left alone. They were granted directly, under the rules
that applied then, and rewriting them into accepted invitations would fabricate a
consent nobody gave.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_invitation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("invitee_id", sa.Integer(), nullable=False),
        sa.Column("invited_by_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # Not tidiness: a pending invitation is a standing grant waiting to be
        # taken, and one forgotten for a year is a way into a project whose
        # owner stopped thinking about it long ago.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invitee_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "invitee_id", name="uq_invitation_project_invitee"),
    )
    op.create_index("ix_invitation_invitee", "project_invitation", ["invitee_id"])


def downgrade() -> None:
    op.drop_index("ix_invitation_invitee", table_name="project_invitation")
    op.drop_table("project_invitation")
