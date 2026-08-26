"""Per-user git credentials, sealed like provider keys.

The clone dialog promised "the deploy's git credentials" — credentials that had
no storage, no env var, and no way to exist. A shared one in the environment
would be wrong anyway, for the same reason there is no shared ANTHROPIC_API_KEY:
every user would push and pull as the operator. So they are per-user rows,
sealed under each user's master key, with the endpoint name as AES-GCM
associated data.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "git_endpoint",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("username", sa.String(128), nullable=True),
        sa.Column("nonce", sa.LargeBinary(12), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "name", name="uq_git_endpoint_user_name"),
    )
    op.create_index("ix_git_endpoint_user", "git_endpoint", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_git_endpoint_user", table_name="git_endpoint")
    op.drop_table("git_endpoint")
