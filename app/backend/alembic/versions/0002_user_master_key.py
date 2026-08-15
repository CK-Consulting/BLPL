"""Per-user master key, its unlock slots, and the onboarding flag.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-15

Provider keys were sealed under the server key, which meant the operator could
read them. The onboarding screen promises otherwise, so the sealing moves to a
key derived from the user's own passphrase. Nothing is re-sealed here — at the
time of this revision no provider key had been stored — but a later revision
cannot assume that, so any future change of sealing key needs a data migration
rather than a schema one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("profile_completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "user_master_key",
        sa.Column("user_id", sa.Integer(), primary_key=True),
        # Argon2id parameters travel with the salt: an unlock must use exactly
        # the ones the wrapping was made with, so raising the cost for new users
        # must not lock out existing ones.
        sa.Column("kdf_salt", sa.LargeBinary(length=16), nullable=False),
        sa.Column("kdf_time_cost", sa.Integer(), nullable=False),
        sa.Column("kdf_memory_kib", sa.Integer(), nullable=False),
        sa.Column("kdf_parallelism", sa.Integer(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )

    # The PRF slot. Deliberately created empty: every row wraps the same master
    # key the passphrase wraps, so adding a passkey later is an INSERT rather
    # than a migration over everyone's encrypted data.
    op.create_table(
        "user_key_credential",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("credential_id", sa.String(length=512), nullable=False),
        sa.Column("prf_salt", sa.LargeBinary(length=32), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("credential_id", name="uq_credential_id"),
    )
    op.create_index("ix_user_key_credential_user", "user_key_credential", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_key_credential_user", table_name="user_key_credential")
    op.drop_table("user_key_credential")
    op.drop_table("user_master_key")
    op.drop_column("users", "profile_completed_at")
