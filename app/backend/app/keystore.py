"""One user's provider keys: read, write, delete.

Replaces the vault's install-wide secret table. The only structural difference is
the one that matters — every function here takes a user, and there is no way to
ask for "the" key for an endpoint without saying whose.

There is deliberately no environment fallback. A shared ANTHROPIC_API_KEY in the
server's environment would mean every signed-in user silently spending the
operator's quota under the operator's account, which is not a default anyone can
consent to. Users bring their own keys, or route the task to something keyless
(ollama, or an OpenAI-compatible endpoint with auth = "none").

Sealing is the same AES-GCM used before, under the server key from
app/serverkey.py, with the endpoint name as associated data. The plaintext exists
only inside a request, on its way to a stage subprocess's environment.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import serverkey, vault
from .models import ProviderKey, User


@dataclass(frozen=True)
class KeyMeta:
    """What is safe to tell a UI: that a key exists, and when it changed."""

    endpoint: str
    updated_at: str


class KeystoreError(RuntimeError):
    """The keystore cannot operate — almost always a missing server key."""


def _server_key(data_root) -> bytes:
    key = serverkey.load(data_root)
    if key is None:
        raise KeystoreError(
            "no server key: set BLPL_SERVER_KEY or let the app create "
            f"{serverkey.key_path(data_root)}. Provider keys cannot be sealed without one."
        )
    return key


def put(session: Session, data_root, user: User, endpoint: str, value: str) -> None:
    """Store (or replace) this user's key for one endpoint."""
    if not value.strip():
        raise ValueError("an empty key is not a key")
    nonce, ciphertext = vault.encrypt_secret(_server_key(data_root), endpoint, value)
    existing = session.scalar(
        select(ProviderKey).where(
            ProviderKey.user_id == user.id, ProviderKey.endpoint == endpoint
        )
    )
    if existing is None:
        session.add(
            ProviderKey(user_id=user.id, endpoint=endpoint, nonce=nonce, ciphertext=ciphertext)
        )
    else:
        # Replace in place so the row's identity — and anything referencing it —
        # survives a key rotation.
        existing.nonce, existing.ciphertext = nonce, ciphertext


def get(session: Session, data_root, user: User, endpoint: str) -> str | None:
    """This user's plaintext key for one endpoint, or None.

    The ONLY callers are the ones injecting it into a subprocess environment or
    an in-process LLM client. It must never reach an HTTP response body.
    """
    row = session.scalar(
        select(ProviderKey).where(
            ProviderKey.user_id == user.id, ProviderKey.endpoint == endpoint
        )
    )
    if row is None:
        return None
    return vault.decrypt_secret(_server_key(data_root), endpoint, row.nonce, row.ciphertext)


def endpoints_with_keys(session: Session, user: User) -> set[str]:
    """Which endpoints this user can authenticate.

    Reads presence, never values, so it needs no server key — which means the run
    preflight can answer "will this work" even on an install whose server key is
    misconfigured, and say so precisely instead of failing at the first decrypt.
    """
    rows = session.scalars(
        select(ProviderKey.endpoint).where(ProviderKey.user_id == user.id)
    )
    return set(rows)


def list_meta(session: Session, user: User) -> list[KeyMeta]:
    rows = session.scalars(
        select(ProviderKey).where(ProviderKey.user_id == user.id).order_by(ProviderKey.endpoint)
    )
    return [KeyMeta(endpoint=r.endpoint, updated_at=r.updated_at.isoformat()) for r in rows]


def delete(session: Session, user: User, endpoint: str) -> bool:
    """Forget a key. Needs no server key — you can discard what you cannot read."""
    row = session.scalar(
        select(ProviderKey).where(
            ProviderKey.user_id == user.id, ProviderKey.endpoint == endpoint
        )
    )
    if row is None:
        return False
    session.delete(row)
    return True
