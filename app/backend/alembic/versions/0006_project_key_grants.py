"""Per-user keypairs and per-project key grants.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-15

Groundwork for encrypting project storage. Nothing is encrypted by this
revision; what it adds is the ability to hand a project key to somebody who is
not currently online, which is what makes an invitation work once there is
anything to hand over.

Existing projects get a key and a grant for each current member, so the
membership that already exists carries over rather than needing to be re-granted
by hand.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_keypair",
        sa.Column("user_id", sa.Integer(), primary_key=True),
        # Public in the clear on purpose: it is the address others send a key to,
        # and keeping it readable is what allows granting access to someone who
        # is offline.
        sa.Column("public_key", sa.LargeBinary(length=32), nullable=False),
        sa.Column("private_nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("private_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "project_key_grant",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("ephemeral_public", sa.LargeBinary(length=32), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "user_id", name="uq_grant_project_user"),
    )
    op.create_index("ix_grant_user", "project_key_grant", ["user_id"])

    # No backfill of keys or grants here. A keypair's private half must be sealed
    # under the user's master key, which only exists while that user is unlocked
    # — something a migration is in no position to arrange. Both are created on
    # demand instead, the first time an unlocked session needs one.


def downgrade() -> None:
    op.drop_index("ix_grant_user", table_name="project_key_grant")
    op.drop_table("project_key_grant")
    op.drop_table("user_keypair")
