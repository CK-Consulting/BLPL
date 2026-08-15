"""Baseline: users and their provider keys.

Revision ID: 0001
Create Date: 2026-08-15

Nothing is migrated *into* this. The SQLite vault it replaces was single-tenant
and, at the point of the switch, held no secrets — so this starts empty rather
than carrying anything across. A later revision cannot assume that; this one can.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        # The Clerk subject, not the email — an address can change or be shared
        # across sign-in methods, and keying on it would merge two people's rows.
        sa.Column("clerk_user_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("clerk_user_id", name="uq_users_clerk_user_id"),
    )
    op.create_index("ix_users_clerk_user_id", "users", ["clerk_user_id"])

    op.create_table(
        "provider_key",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        # Ciphertext only. There is deliberately no column a plaintext key could
        # sit in, so no query and no dump can produce one.
        sa.Column("nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "endpoint", name="uq_provider_key_user_endpoint"),
    )
    op.create_index("ix_provider_key_user", "provider_key", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_provider_key_user", table_name="provider_key")
    op.drop_table("provider_key")
    op.drop_index("ix_users_clerk_user_id", table_name="users")
    op.drop_table("users")
