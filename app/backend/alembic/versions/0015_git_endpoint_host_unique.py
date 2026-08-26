"""One git credential per host, per user.

Matching by host is the only selection mechanism the store has — the clone form
is one URL field, and there is no account picker. Two rows for one host would
make every clone, pull and push a coin toss between identities, and a push
under the wrong account is the failure that looks like success. The store
refuses the second row with a message; this constraint is the backstop for any
path that skips the check.

Duplicates cannot exist yet (0014 and this ship in the same PR), but the
migration clears them defensively — keeping the most recently updated row,
which is the one whose secret was rotated last — because a constraint that can
fail to apply is worse than none.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM git_endpoint a USING git_endpoint b "
        "WHERE a.user_id = b.user_id AND a.host = b.host "
        "AND (a.updated_at, a.id) < (b.updated_at, b.id)"
    )
    op.create_unique_constraint(
        "uq_git_endpoint_user_host", "git_endpoint", ["user_id", "host"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_git_endpoint_user_host", "git_endpoint", type_="unique")
