"""The Postgres connection, and the schema that makes this app multi-user.

Why a database at all, when a SQLite vault worked: the vault is structurally
single-tenant. One row of key material, one secret table with no owner column,
and one DEK derived from one passphrase. There is no version of "several people
use this server, each with their own provider keys" that fits inside it — the
question is not storage volume, it is that a shared DEK cannot express a private
key. Postgres is here for the ownership model, not the scale.

What protects a key at rest
---------------------------

Clerk answers "who is this". It holds no encryption key for our data, so it
cannot answer "what decrypts this column" — and because the user never gives us
a secret, there is nothing to derive a per-user key *from*. So keys are sealed
with a server-held key (BLPL_SERVER_KEY) using the same AES-GCM helpers the
vault used, with the endpoint name as associated data.

State the consequence plainly: a database dump alone is inert, a dump plus the
server key is every user's keys, and the server operator can always read them.
This is the ordinary posture for a hosted tool and it is what the vault's second
wrapping already committed to — but a user typing a key into Settings is
trusting the operator, not just the software, and the docs say so.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Default matches the compose service so a plain `docker compose up` works with
# no configuration. psycopg (v3) rather than psycopg2: it is the maintained line
# and ships a binary wheel, which matters on the emulated amd64 build.
DATABASE_URL = os.environ.get(
    "BLPL_DATABASE_URL", "postgresql+psycopg://blpl:blpl@db:5432/blpl"
)

# pool_pre_ping because the database and the app restart independently — without
# it, the first request after a Postgres restart fails on a stale connection
# rather than transparently reconnecting.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def session_scope() -> Iterator[Session]:
    """FastAPI dependency: one session per request, committed or rolled back.

    Explicit rollback on the error path rather than relying on the connection
    being returned to the pool: a half-applied write that silently persists
    because nobody rolled back is the kind of bug that shows up as data that
    "cannot have happened".
    """
    session = SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
