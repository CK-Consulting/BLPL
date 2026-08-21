"""Rename the datasheet_vision task route to plain vision.

The task existed because one tool needed a model that could see. That is not a
property of datasheets; it is a property of the request. Naming the route after
its first caller meant a second thing needing vision would either reuse a route
named for something else or add a near-duplicate beside it — and the settings
screen would list both, with no way to tell which mattered.

So the route is now what it always was: "give me a model that can see". One
route, one meaning, and a new caller needing vision has somewhere to go.

Renamed rather than added-and-deprecated. There is exactly one call site in the
code, the routes are per-user configuration rather than history, and leaving the
old name behind would mean two rows that mostly agree and occasionally do not —
which is the failure the rename exists to prevent.

The unique constraint is on (user_id, task), so a user who somehow has both
rows would collide. That is resolved by keeping the one that was configured
deliberately: an explicit `vision` row wins over a `datasheet_vision` one.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop any datasheet_vision row for a user who already has a vision row.
    # Nothing is lost that was not already superseded.
    op.execute(
        """
        DELETE FROM llm_task_route a
         WHERE a.task = 'datasheet_vision'
           AND EXISTS (
                 SELECT 1 FROM llm_task_route b
                  WHERE b.user_id = a.user_id AND b.task = 'vision'
               )
        """
    )
    op.execute("UPDATE llm_task_route SET task = 'vision' WHERE task = 'datasheet_vision'")


def downgrade() -> None:
    op.execute("UPDATE llm_task_route SET task = 'datasheet_vision' WHERE task = 'vision'")
