"""Keys that can be handed to someone who is not here.

A project gets one random key. Every member holds that same key, wrapped to
them individually — which is what lets an owner grant access to somebody who is
offline, asleep, or has not yet decided whether to accept.

That property is the whole reason for asymmetric crypto here. With only the
symmetric machinery in app/userkey.py, granting access would need *both* people
present and unlocked at the same instant: the owner to read the project key, the
recipient to have theirs derived. Invitations would then be impossible in any
useful form — you could not offer access and let someone take it up tomorrow.

The construction is a sealed box:

    ephemeral X25519 keypair ─┐
                              ├─ ECDH ─▶ HKDF ─▶ AES-GCM key ─▶ wraps project key
    recipient's public key ───┘

The ephemeral public key is stored beside the ciphertext; the recipient combines
it with their private key to arrive at the same shared secret. A fresh ephemeral
key per wrap means two grants of the same project key look unrelated, and the
sender needs nothing of their own to perform the wrap — anyone holding the
project key can grant it onward, which is exactly what a member list is.

What this does not do is hide the project key from the server while a session is
open. The plaintext passes through this process to decrypt a workspace. As
elsewhere, the crypto keeps keys out of the database and off the disk; it does
not keep them from a running server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from .vault import VaultError

_KEY_LEN = 32
_NONCE_LEN = 12
# Domain separation: the same ECDH secret must not produce the same AES key in
# some other context that might later reuse these keypairs.
_HKDF_INFO = b"blpl-project-key-grant"


class GrantError(VaultError):
    """A wrapped project key could not be opened for this recipient."""


@dataclass(frozen=True)
class SealedTo:
    """A project key wrapped for one recipient."""

    ephemeral_public: bytes
    nonce: bytes
    ciphertext: bytes


def new_project_key() -> bytes:
    return os.urandom(_KEY_LEN)


def new_keypair() -> tuple[bytes, bytes]:
    """(private, public), both raw 32-byte X25519.

    Raw rather than PEM: these are stored in a BYTEA column and read by this
    module alone, so the extra encoding would buy nothing and would make the
    column's contents harder to reason about.
    """
    private = X25519PrivateKey.generate()
    return (
        private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )


def public_of(private_bytes: bytes) -> bytes:
    return (
        X25519PrivateKey.from_private_bytes(private_bytes)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )


def seal_to(recipient_public: bytes, secret: bytes) -> SealedTo:
    """Wrap ``secret`` so only the holder of the matching private key can open it.

    Needs nothing from the sender — no key of their own, no signature. Anyone
    already holding the project key can pass it on, which is precisely what
    being a member means, and it keeps granting from depending on who is
    currently unlocked.
    """
    _check(recipient_public, "recipient public key")
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient_public))
    key = _derive(shared)
    nonce = os.urandom(_NONCE_LEN)
    return SealedTo(
        ephemeral_public=ephemeral.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        nonce=nonce,
        ciphertext=AESGCM(key).encrypt(nonce, secret, _HKDF_INFO),
    )


def open_sealed(recipient_private: bytes, sealed: SealedTo) -> bytes:
    """Recover the secret. Raises GrantError if this is not the right key."""
    _check(recipient_private, "recipient private key")
    shared = X25519PrivateKey.from_private_bytes(recipient_private).exchange(
        X25519PublicKey.from_public_bytes(sealed.ephemeral_public)
    )
    try:
        return AESGCM(_derive(shared)).decrypt(sealed.nonce, sealed.ciphertext, _HKDF_INFO)
    except InvalidTag as exc:
        raise GrantError("this grant was not sealed to you") from exc


def _derive(shared: bytes) -> bytes:
    """ECDH output is not uniform enough to use as a key directly — HKDF is what
    turns a curve point into 32 bytes safe to hand AES-GCM."""
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_LEN, salt=None, info=_HKDF_INFO).derive(
        shared
    )


def _check(raw: bytes, what: str) -> None:
    if len(raw) != _KEY_LEN:
        raise GrantError(f"{what} must be {_KEY_LEN} bytes, got {len(raw)}")
