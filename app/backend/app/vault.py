"""Encrypted secret storage for the BLPL app.

The threat model is concrete: the app runs on a VM you roam back to from any
workstation, and it holds your LLM provider API keys. If someone reads the
SQLite file off that VM's disk — a stray backup, a snapshot, a stolen volume —
they must not get your keys. So keys are never on disk in the clear, and the
thing that decrypts them (the passphrase) is never on disk at all.

The scheme, single-user:

    passphrase ──Argon2id(salt)──▶ KEK ──AES-GCM──▶ wraps a random DEK
                                                     │
    each provider key ──AES-GCM(DEK, aad=provider)──▶ ciphertext on disk

Two keys, on purpose. The *KEK* is derived from the passphrase and only ever
wraps the DEK. The *DEK* is random and does the actual work. That indirection
buys one thing that matters: changing the passphrase re-wraps one 32-byte DEK
instead of re-encrypting every stored secret, and the secrets' ciphertext never
changes. The DEK exists in plaintext only in RAM, only while unlocked.

There is no passphrase verifier stored anywhere. A wrong passphrase derives a
wrong KEK, which fails to unwrap the DEK — AES-GCM's authentication tag simply
does not verify — and that failure *is* the "wrong passphrase" signal. Nothing
to brute-force offline beyond Argon2id itself.

AES-GCM's own rule — never reuse a (key, nonce) pair — is the sharp edge here.
Every encrypt generates a fresh random 12-byte nonce and stores it beside the
ciphertext. The AAD binds each secret to its provider name, so a ciphertext
lifted from the ``openai`` row cannot be replayed into the ``anthropic`` row.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Argon2id parameters. These are cost knobs, not correctness knobs — raise them
# and old material still verifies because the parameters are stored alongside
# the salt (see KdfParams). Defaults chosen for an interactive unlock on a
# server: ~64 MiB, 3 passes. OWASP's 2024 floor is 19 MiB/2 passes; we sit above
# it because unlock happens rarely (once per session), so latency is cheap.
_ARGON_TIME_COST = 3
_ARGON_MEMORY_KIB = 64 * 1024
_ARGON_PARALLELISM = 4
_KEY_LEN = 32  # 256-bit keys for both KEK and DEK
_SALT_LEN = 16
_NONCE_LEN = 12  # AES-GCM standard nonce size


class VaultError(RuntimeError):
    """Base for vault failures."""


class WrongPassphrase(VaultError):
    """The passphrase did not unwrap the DEK. Indistinguishable from a corrupt
    wrapped-DEK blob, and deliberately so — we don't tell an attacker which."""


@dataclass(frozen=True)
class KdfParams:
    """Everything needed to re-derive the KEK from the passphrase, except the
    passphrase. Stored in the clear — none of it is secret, and future unlocks
    must use the exact parameters the material was created with."""

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
class WrappedDek:
    """The DEK, encrypted under the KEK. Safe to persist."""

    nonce: bytes
    ciphertext: bytes  # includes the GCM tag


def init_vault(passphrase: str) -> tuple[KdfParams, WrappedDek]:
    """Create fresh key material for a first-time setup.

    Returns the KDF parameters and the wrapped DEK, both of which the caller
    persists. The plaintext DEK is generated, used to wrap, and dropped — it is
    never returned here, because at init time nobody is unlocking yet.
    """
    params = KdfParams(salt=os.urandom(_SALT_LEN))
    kek = params.derive(passphrase)
    dek = os.urandom(_KEY_LEN)
    wrapped = _wrap_dek(kek, dek)
    return params, wrapped


def unlock(passphrase: str, params: KdfParams, wrapped: WrappedDek) -> bytes:
    """Return the plaintext DEK, or raise WrongPassphrase.

    The returned DEK is live secret material. The caller holds it in memory for
    the duration of a session and never writes it anywhere.
    """
    kek = params.derive(passphrase)
    try:
        return AESGCM(kek).decrypt(wrapped.nonce, wrapped.ciphertext, b"blpl-dek")
    except InvalidTag as exc:
        raise WrongPassphrase("passphrase did not unwrap the data key") from exc


def change_passphrase(
    old: str, new: str, params: KdfParams, wrapped: WrappedDek
) -> tuple[KdfParams, WrappedDek]:
    """Re-wrap the existing DEK under a new passphrase.

    The DEK does not change, so every stored secret stays valid and untouched —
    only the wrapping and the salt are replaced. Verifies the old passphrase by
    unwrapping first; a wrong old passphrase raises WrongPassphrase.
    """
    dek = unlock(old, params, wrapped)
    new_params = KdfParams(salt=os.urandom(_SALT_LEN))
    new_kek = new_params.derive(new)
    return new_params, _wrap_dek(new_kek, dek)


def encrypt_secret(dek: bytes, provider: str, value: str) -> tuple[bytes, bytes]:
    """Encrypt one provider secret. Returns (nonce, ciphertext).

    The provider name is the AES-GCM associated data: it is authenticated but not
    encrypted, so a ciphertext is cryptographically bound to the row it belongs
    to. Move the ``openai`` ciphertext into the ``anthropic`` row and decryption
    fails rather than silently handing back the wrong key.
    """
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(dek).encrypt(nonce, value.encode("utf-8"), provider.encode("utf-8"))
    return nonce, ciphertext


def decrypt_secret(dek: bytes, provider: str, nonce: bytes, ciphertext: bytes) -> str:
    """Decrypt one provider secret. Raises VaultError if the DEK is wrong, the
    data is corrupt, or the provider name does not match what was sealed in."""
    try:
        plaintext = AESGCM(dek).decrypt(nonce, ciphertext, provider.encode("utf-8"))
    except InvalidTag as exc:
        raise VaultError(f"could not decrypt secret for {provider!r}") from exc
    return plaintext.decode("utf-8")


def _wrap_dek(kek: bytes, dek: bytes) -> WrappedDek:
    nonce = os.urandom(_NONCE_LEN)
    return WrappedDek(nonce=nonce, ciphertext=AESGCM(kek).encrypt(nonce, dek, b"blpl-dek"))
