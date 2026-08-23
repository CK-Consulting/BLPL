"""Let an endpoint state its context window and output cap.

Both were added to the dataclass with a "declared always wins" rule and no way
to declare them: not on the request body, not in the database, not in the file
config. So the sizing logic that decides what to leave out of a request always
fell back to inference — which is exactly wrong for the deployments that need
the override most. A self-hosted model's window is a property of how the server
was started, not of the weights, and Ollama publishes neither number.

Both nullable, because null is a real state and means "work it out": discovered
from the server where one will say, inferred from the model id otherwise.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_endpoint", sa.Column("context_tokens", sa.Integer(), nullable=True))
    op.add_column("llm_endpoint", sa.Column("max_output_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_endpoint", "max_output_tokens")
    op.drop_column("llm_endpoint", "context_tokens")
