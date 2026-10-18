"""A project's own idle limit before it is sealed again.

Null is the deployment default (idlepolicy.DEFAULT_MINUTES, an hour).

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project", sa.Column("idle_seal_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("project", "idle_seal_minutes")
