"""Sealing a secret so the database does not hold it in the clear.

Once this file was a whole vault: a passphrase, an Argon2id KEK, a wrapped data
key, sessions holding it in RAM. All of that existed because the app was
single-user and the passphrase was both the front door and the key. Clerk is the
front door now, and Clerk supplies no key material — it proves who you are and
holds nothing that could decrypt your data. Since the user never gives us a
secret, there is nothing to derive a per-user key from, so what is left is the
part that was never about authentication: authenticated encryption under a
server-held key.

    provider key ──AES-GCM(server key, aad=endpoint)──▶ ciphertext in Postgres

Be exact about what that buys. A stolen database dump is inert on its own. A dump
*plus* the server key is every user's keys, and the operator has both. This is
the ordinary posture for a hosted tool, and it is why app/serverkey.py insists the
key lives outside the database it opens — but a user typing a key into Settings is
trusting the operator, not merely the software, and the docs say so plainly.

AES-GCM's rule — never reuse a (key, nonce) pair — is the sharp edge. Every
encrypt draws a fresh random 12-byte nonce and stores it beside the ciphertext.
The AAD binds each secret to its endpoint name, so a ciphertext lifted from the
``openai`` row cannot be replayed into the ``anthropic`` row, or into another
user's.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12  # AES-GCM standard nonce size
_KEY_LEN = 32  # 256-bit


class VaultError(RuntimeError):
    """A secret could not be sealed or opened."""


def encrypt_secret(key: bytes, endpoint: str, value: str) -> tuple[bytes, bytes]:
    """Seal one secret. Returns (nonce, ciphertext).

    The endpoint name is the associated data: authenticated but not encrypted, so
    the ciphertext is cryptographically bound to the row it belongs to.
    """
    _check_key(key)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), endpoint.encode("utf-8"))
    return nonce, ciphertext


def decrypt_secret(key: bytes, endpoint: str, nonce: bytes, ciphertext: bytes) -> str:
    """Open one secret. Raises VaultError if the key is wrong, the data is
    corrupt, or the endpoint name does not match what was sealed in."""
    _check_key(key)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, endpoint.encode("utf-8"))
    except InvalidTag as exc:
        raise VaultError(f"could not decrypt the secret for {endpoint!r}") from exc
    return plaintext.decode("utf-8")


def _check_key(key: bytes) -> None:
    """A short or truncated key must fail here rather than silently producing a
    weaker cipher — AESGCM would accept 16 or 24 bytes without comment."""
    if len(key) != _KEY_LEN:
        raise VaultError(f"server key must be {_KEY_LEN} bytes, got {len(key)}")
