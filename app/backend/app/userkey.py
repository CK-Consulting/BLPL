"""Each user's master key, and the slots that can unlock it.

The onboarding screen promises that a passphrase means "you and only you can
access your data". That sentence is only true if the key is derived from
something the server never stores — so this is where it becomes true, and it is
why provider keys moved off the server key and onto this one.

The shape:

    random master key (32 bytes, never leaves memory in the clear)
      ├── wrapped under Argon2id(passphrase)        ← today: recovery, and any
      │                                                device without PRF
      └── wrapped under HKDF(WebAuthn PRF output)   ← next: one touch to unlock

Two slots rather than one, from the start, even though only the first is filled.
Both wrap the *same* master key, so adding the passkey slot later re-encrypts
nothing and removing one does not orphan anything — and a lost passkey is an
inconvenience rather than every project becoming unreadable. That property is
worth having before it is needed, because retrofitting it means a migration over
live user data.

What this does not achieve, stated plainly: stage runs are server-side
subprocesses that need the key to inject into a child environment, so the
derived key reaches this process and lives in its memory for the session. The
passphrase (or the passkey) keeps the key out of the database and off the disk;
it does not keep it away from a running server. Real end-to-end would mean the
browser doing the crypto, which a pipeline that shells out to KiCad cannot do.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .vault import VaultError

# Interactive cost. Unlock happens once per session, not per request, so this
# can sit above OWASP's 2024 floor (19 MiB / 2 passes) without anyone noticing.
_ARGON_TIME_COST = 3
_ARGON_MEMORY_KIB = 64 * 1024
_ARGON_PARALLELISM = 4
_KEY_LEN = 32
_SALT_LEN = 16
_NONCE_LEN = 12

# Distinct associated data per slot, so a blob from one cannot be fed to the
# other. They are the same 32 bytes of plaintext but not interchangeable
# ciphertexts, and a mix-up fails loudly instead of silently.
_AAD_PASSPHRASE = b"blpl-master-passphrase"
_AAD_PRF = b"blpl-master-prf"

MIN_PASSPHRASE_LEN = 10


class WrongPassphrase(VaultError):
    """The passphrase did not unwrap the master key. Indistinguishable from a
    corrupt blob, deliberately — we do not tell a guesser which they hit."""


@dataclass(frozen=True)
class KdfParams:
    """Everything needed to re-derive the KEK except the passphrase. Stored in
    the clear: none of it is secret, and a later unlock must use exactly the
    parameters the material was created with."""

    salt: bytes
    time_cost: int = _ARGON_TIME_COST
    memory_kib: int = _ARGON_MEMORY_KIB
    parallelism: int = _ARGON_PARALLELISM

    def derive(self, passphrase: str) -> bytes:
        return hash_secret_raw(
            secret=passphrase.encode("utf-8"),
            salt=self.salt,
            time_cost=self.time_cost,
            memory_cost=self.memory_kib,
            parallelism=self.parallelism,
            hash_len=_KEY_LEN,
            type=Type.ID,
        )


@dataclass(frozen=True)
class Wrapped:
    nonce: bytes
    ciphertext: bytes


def new_master_key() -> bytes:
    return os.urandom(_KEY_LEN)


def new_params() -> KdfParams:
    return KdfParams(salt=os.urandom(_SALT_LEN))


def wrap_with_passphrase(passphrase: str, params: KdfParams, master: bytes) -> Wrapped:
    _check_passphrase(passphrase)
    return _wrap(params.derive(passphrase), master, _AAD_PASSPHRASE)


def unwrap_with_passphrase(passphrase: str, params: KdfParams, wrapped: Wrapped) -> bytes:
    """The master key, or WrongPassphrase.

    There is no separate verifier stored anywhere: a wrong passphrase derives a
    wrong KEK, AES-GCM's tag fails, and that failure *is* the signal. Nothing to
    attack offline beyond Argon2id itself.
    """
    try:
        return _unwrap(params.derive(passphrase), wrapped, _AAD_PASSPHRASE)
    except InvalidTag as exc:
        raise WrongPassphrase("passphrase did not unwrap your key") from exc


def wrap_with_prf(prf_output: bytes, master: bytes) -> Wrapped:
    """The second slot. Not reachable from the UI yet; the storage and the
    crypto exist so that enabling it later adds a row rather than migrating
    every user's data."""
    return _wrap(_key_from_prf(prf_output), master, _AAD_PRF)


def unwrap_with_prf(prf_output: bytes, wrapped: Wrapped) -> bytes:
    try:
        return _unwrap(_key_from_prf(prf_output), wrapped, _AAD_PRF)
    except InvalidTag as exc:
        raise VaultError("that credential did not unwrap your key") from exc


def _key_from_prf(prf_output: bytes) -> bytes:
    """The authenticator's PRF result is already 32 uniform bytes, but it is used
    verbatim nowhere else — taking it through HKDF keeps this slot's key
    domain-separated from any other use of the same credential."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if len(prf_output) < _KEY_LEN:
        raise VaultError("PRF output too short to derive a key from")
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_LEN, salt=None, info=_AAD_PRF).derive(
        prf_output
    )


def _wrap(kek: bytes, master: bytes, aad: bytes) -> Wrapped:
    nonce = os.urandom(_NONCE_LEN)
    return Wrapped(nonce=nonce, ciphertext=AESGCM(kek).encrypt(nonce, master, aad))


def _unwrap(kek: bytes, wrapped: Wrapped, aad: bytes) -> bytes:
    return AESGCM(kek).decrypt(wrapped.nonce, wrapped.ciphertext, aad)


def _check_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE_LEN:
        raise ValueError(f"passphrase must be at least {MIN_PASSPHRASE_LEN} characters")
