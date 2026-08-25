"""What each project has declared about the shared component library.

Three settings, none of them with a silent default, and a record of which
consent wording was in force when they were agreed.

In the database rather than in the project directory, deliberately. Everything
inside a project is proposable — the design assistant can offer an edit to any
file there, and somebody accepting a diff is not reading it as a permissions
change. A consent setting that a model can propose relaxing is not a consent
setting.

Existing projects get the conservative row: contribute nothing, ask before
consuming, never contribute a component the library does not already hold. They
were created before anyone was asked, and inferring agreement from silence is
the thing this table exists to stop.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contribute", sa.String(32), nullable=False, server_default="never"),
        sa.Column("consume", sa.String(32), nullable=False, server_default="ask"),
        sa.Column(
            "unique_components", sa.String(32), nullable=False, server_default="never_contribute"
        ),
        sa.Column("consent_sha", sa.String(64), nullable=True),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", name="uq_project_policy_project"),
    )
    # Every project that already exists, at the closed end. No consent_sha,
    # because nobody consented to anything — the columns say "not yet asked"
    # rather than pretending to an answer.
    op.execute(
        "INSERT INTO project_policy (project_id, contribute, consume, unique_components) "
        "SELECT id, 'never', 'ask', 'never_contribute' FROM project"
    )


def downgrade() -> None:
    op.drop_table("project_policy")
