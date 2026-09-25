"""Remember which workspaces are open, so a crash cannot leave one plaintext.

Sealing was driven entirely by a process-local dict. Every path that ends in a
user saying "I'm done" worked; the path where the server dies first did not.
The dict went with the process, the idle sweeper had nothing to sweep, and the
unsealed project stayed readable on disk until somebody happened to open and
lock it again — which is the encryption's central claim quietly suspended.

The row carries the project key wrapped under the server key, because sealing
needs a key that is otherwise reachable only through a user's master key, and
after a restart there is no user. See models.OpenWorkspaceRow for why that
wrapping adds no exposure: the row exists only while the project is already
plaintext beside it, and is deleted when the workspace is sealed.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "open_workspace",
        sa.Column("workspace", sa.String(length=128), primary_key=True),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=False),
        sa.Column("holders", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_touched", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    # Dropping this does not re-seal anything; it only forgets what is open.
    # Seal before downgrading, or the next boot has no record to act on.
    op.drop_table("open_workspace")
