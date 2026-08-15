"""Alembic's entry point, wired to the app's own engine.

The connection string is taken from app.db rather than alembic.ini on purpose:
one place decides which database this app talks to, so a migration cannot run
against a different one than the server does. That mismatch is quiet and awful —
the app looks like it lost your data when it merely looked somewhere else.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.db import engine
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it — for review before a change
    lands on a database with real user keys in it."""
    context.configure(
        url=str(engine.url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # compare_type so a widened column shows up in autogenerate. Off by
            # default, and its absence is why "the migration was empty" happens.
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
