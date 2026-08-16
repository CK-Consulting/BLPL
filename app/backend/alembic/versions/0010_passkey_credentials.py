"""Give passkey credentials a public key and a signature counter.

The table has been there since the master key gained two wrapping slots, holding
everything needed to *unwrap* with a passkey and nothing needed to *verify* one.
That was fine while the slot was unreachable and is not now: without the public
key there is no way to check an assertion, and the endpoint would take any
well-formed request and try to unwrap with whatever it was handed.

Both columns land with defaults and no backfill, because the table is empty by
construction — the ceremony that writes rows ships in the same change.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_key_credential",
        # server_default must be SQL, not a Python bytes literal: alembic passes
        # it through to the DDL, and bytes raise before the migration even runs.
        sa.Column(
            "public_key", sa.LargeBinary(), nullable=False, server_default=sa.text("''::bytea")
        ),
    )
    op.add_column(
        "user_key_credential",
        sa.Column("sign_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("user_key_credential", "sign_count")
    op.drop_column("user_key_credential", "public_key")
