"""The LLM endpoint registry and task routing become per-user.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-15

Provider *keys* became per-user in 0001/0002, but the registry that names the
endpoints those keys belong to stayed in the install-wide blpl.toml. That
mismatch is invisible with one user and wrong with two: the second person to
finish setup rewrote the first person's endpoints and task chains, and the
symptom — someone else's provider silently becoming your default — is not
something anyone would think to look for.

Deliberately NOT migrated from the file: the projects registry and the MCP
servers. A project is not yet owned by anyone, so moving it per-user here would
invent an ownership model half a phase early and in the wrong place.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_endpoint",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("auth", sa.String(length=16), nullable=False, server_default="vault"),
        # Nullable on purpose: null is "infer from kind", which is a different
        # statement from "cannot see". A local server's vision support is not
        # guessable, and a wrong guess makes the datasheet extractor silently
        # read nothing.
        sa.Column("vision", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "name", name="uq_llm_endpoint_user_name"),
    )
    op.create_index("ix_llm_endpoint_user", "llm_endpoint", ["user_id"])

    op.create_table(
        "llm_task_route",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("task", sa.String(length=64), nullable=False),
        # The ordered chain as one value. Index 0 is tried first, so the order
        # IS the content — keeping it atomic beats a row per position, where a
        # partial write would reorder a fallback chain without failing.
        sa.Column("endpoints", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "task", name="uq_llm_task_user_task"),
    )

    _copy_file_registry_to_existing_users()


def _copy_file_registry_to_existing_users() -> None:
    """Give everyone who already set up a copy of what the shared file held.

    A schema change that relocates data owes the people whose data it is a way
    across. Without this, anyone already onboarded would sign in to find their
    provider gone and their runs refusing for want of an endpoint — which reads
    as the app breaking, not as a migration.

    Every existing user gets the same copy, because the file *was* everyone's:
    there is no record of who configured what, and that indistinguishability is
    the exact problem this revision fixes. New users get nothing from here; they
    configure their own during setup.
    """
    import json
    import os
    from pathlib import Path

    config_path = Path(os.environ.get("BLPL_CONFIG", "/app/data/blpl.toml"))
    if not config_path.exists():
        return

    from app import appconfig

    cfg = appconfig.load(config_path)
    if not cfg.endpoints:
        return

    conn = op.get_bind()
    user_ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM users"))]
    if not user_ids:
        return

    for user_id in user_ids:
        for name, ep in cfg.endpoints.items():
            conn.execute(
                sa.text(
                    "INSERT INTO llm_endpoint "
                    "(user_id, name, kind, model, base_url, auth, vision, created_at, updated_at) "
                    "VALUES (:u, :n, :k, :m, :b, :a, :v, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "u": user_id,
                    "n": name,
                    "k": ep.kind,
                    "m": ep.model,
                    "b": ep.base_url,
                    "a": ep.auth,
                    "v": ep.vision,
                },
            )
        for task, chain in cfg.tasks.items():
            if not chain:
                continue
            conn.execute(
                sa.text(
                    "INSERT INTO llm_task_route (user_id, task, endpoints, updated_at) "
                    "VALUES (:u, :t, :e, CURRENT_TIMESTAMP)"
                ),
                {"u": user_id, "t": task, "e": json.dumps(list(chain))},
            )


def downgrade() -> None:
    op.drop_table("llm_task_route")
    op.drop_index("ix_llm_endpoint_user", table_name="llm_endpoint")
    op.drop_table("llm_endpoint")
