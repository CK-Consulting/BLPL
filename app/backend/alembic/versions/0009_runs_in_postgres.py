"""Runs become rows in Postgres and a queue workers claim from.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-15

Runs lived in SQLite with an in-memory fan-out, which is what pinned the server
to `--workers 1`: a second API process could not see the first's runs, and an SSE
reader that landed on the wrong process saw nothing at all. The fix is not to
share the in-memory queue — it is for the API to stop executing anything. It
enqueues; workers claim and run.

Claiming is SELECT ... FOR UPDATE SKIP LOCKED. Two workers racing for one row is
the ordinary case rather than an error: one wins and the other moves on.

The old runs.db is left where it is. Its rows are history — which stage ran, when,
and what it exited with — and inventing project and user ids for them would
attribute past work to whoever happens to own things now. The log files it points
at are still on disk if anyone wants them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        # Kept when the user is deleted rather than cascading: a run that
        # happened is a fact about the past, and losing the record because
        # someone left would quietly rewrite a project's history.
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("cmd", sa.JSON(), nullable=False),
        # The subprocess environment, sealed under the server key. It carries
        # provider API keys the worker cannot get any other way — they live under
        # the user's master key, which exists only in an unlocked API session.
        sa.Column("env_nonce", sa.LargeBinary(length=12), nullable=True),
        sa.Column("env_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("claimed_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    # The claim query filters on status and orders by age, and runs on every
    # worker poll.
    op.create_index("ix_run_status_created", "run", ["status", "created_at"])
    op.create_index("ix_run_project", "run", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_run_project", table_name="run")
    op.drop_index("ix_run_status_created", table_name="run")
    op.drop_table("run")
