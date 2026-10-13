"""Per-run egress grants and the administrators' egress host list.

Squid now authenticates every request a workspace makes with a credential
issued for one run or one git operation, and asks the control plane whether
that grant may reach the host: the shipped defaults, the hosts administrators
added here, and the hosts of the LLM endpoints in the run's own chain. This
replaces an allowlist file an operator edited and Squid had to be reloaded
for.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "egress_grant",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("token_sha256", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("project", sa.String(128), nullable=False, server_default=""),
        sa.Column("run_id", sa.String(32), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("hosts", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_egress_grant_expires_at", "egress_grant", ["expires_at"])
    op.create_table(
        "egress_host",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("host", sa.String(253), nullable=False, unique=True),
        sa.Column("added_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "egress_host_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("host", sa.String(253), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("email", sa.String(320), nullable=False, server_default=""),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("egress_host_event")
    op.drop_table("egress_host")
    op.drop_index("ix_egress_grant_expires_at", table_name="egress_grant")
    op.drop_table("egress_grant")
