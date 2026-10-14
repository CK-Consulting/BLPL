"""Decide whether a PDF may be rendered by its content hash, not its path.

Any `.pdf` outside `retrieved/` used to be served `application/pdf`, inline,
whatever had put it there. This table records what the backend's own
inspection found for exact bytes, per project, and the blob route renders a
PDF inline only when the bytes it is sending have a clean verdict from the
current inspector. See models.PdfVerdict.

Nothing is seeded here. The in-tree ledgers live in project directories that
may be sealed when this runs, and a ledger is committed project content that a
pull can rewrite, so it is not this backend's record to accept wholesale. Its
held rows are imported on first sight instead, and everything else waits for
an inspection.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pdf_verdict",
        sa.Column(
            "project_id",
            sa.Integer(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sha256", sa.String(length=64), primary_key=True),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("inspector", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("inspection_state", sa.String(length=24), nullable=False, server_default=""),
        sa.Column("scanner", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("scan_state", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("reasons", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column(
            "decided_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    # Dropping it only forgets verdicts; every PDF then downloads until inspected.
    op.drop_table("pdf_verdict")
