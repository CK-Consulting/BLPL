"""One git credential per host *and access method*, per user.

0015 made a host unique per user, so a user could hold either an SSH key or an
HTTPS token for github.com, never both. Matching had the same blind spot: both
normalise to the same host and port once each protocol's default port is
dropped, so saving a new HTTPS token for an https:// remote found the SSH row
and overwrote it in place, deleting the key every SSH remote used (Codex,
PR #19). A token is no use to ssh and a key no use to HTTPS; they are two
credentials, and the constraint now says so. Every existing row already
satisfies the wider constraint, so nothing is deleted.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_git_endpoint_user_host", "git_endpoint", type_="unique")
    op.create_unique_constraint(
        "uq_git_endpoint_user_host_method", "git_endpoint", ["user_id", "host", "method"]
    )


def downgrade() -> None:
    # Back to one row per host: where a host has both, the HTTPS row goes —
    # it is the newer kind here, and a token can be issued again; an SSH key
    # may be the only copy of its private half.
    op.execute(
        "DELETE FROM git_endpoint a USING git_endpoint b "
        "WHERE a.user_id = b.user_id AND a.host = b.host "
        "AND a.method = 'https_token' AND b.method = 'ssh_key'"
    )
    op.drop_constraint("uq_git_endpoint_user_host_method", "git_endpoint", type_="unique")
    op.create_unique_constraint("uq_git_endpoint_user_host", "git_endpoint", ["user_id", "host"])
