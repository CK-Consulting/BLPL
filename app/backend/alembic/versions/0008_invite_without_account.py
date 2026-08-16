"""Inviting someone who has no account yet.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-15

Two changes, and the second is the interesting one.

``users.clerk_user_id`` becomes nullable so an invitation has something to point
at before the invitee exists. Such a row is a *placeholder*: it holds an email
and nothing else, and is claimed by the first Clerk account that proves control
of that address. Clerk requires a verification code to sign up with an email, so
"proves control" is doing real work rather than trusting a string.

The project key still has to reach them, and at invite time there is no keypair
of theirs to seal it to. So it is wrapped under a key derived from a secret that
exists nowhere but the emailed link, and re-sealed to their real key the moment
they redeem it. That is a deliberate weakening: anyone who reads that mailbox can
take the project. It is bounded by a 24-hour expiry and by the wrapped copy being
destroyed on first use.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column("clerk_user_id", existing_type=sa.String(length=255), nullable=True)
    op.create_index("ix_users_email", "users", ["email"])

    with op.batch_alter_table("project_invitation") as batch:
        # Nullable: only invitations to someone without an account carry one.
        # An invitee who already has a keypair gets the key sealed directly to
        # it, which is strictly better and needs no secret in an email.
        batch.add_column(sa.Column("key_nonce", sa.LargeBinary(length=12), nullable=True))
        batch.add_column(sa.Column("key_ciphertext", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("project_invitation") as batch:
        batch.drop_column("key_ciphertext")
        batch.drop_column("key_nonce")
    op.drop_index("ix_users_email", table_name="users")
    # Deliberately not restoring NOT NULL: placeholders may exist by now, and
    # failing a downgrade is better than deleting the rows that would block it.
