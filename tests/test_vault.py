"""The encrypted vault holds the user's LLM API keys on a roaming server.

These tests are about the security properties, not just the happy path: a wrong
passphrase must fail, a tampered ciphertext must fail, a secret sealed for one
provider must not decrypt as another, and — the property the whole two-key design
exists for — changing the passphrase must not disturb the stored secrets.

argon2id is deliberately slow, so the module's default cost would make this suite
crawl. Every test overrides KdfParams with a cheap cost; the crypto path is
identical, only the work factor changes.
"""

from __future__ import annotations

import dataclasses

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app import vault  # noqa: E402


def _cheap(params: vault.KdfParams) -> vault.KdfParams:
    """Same salt, negligible cost — keeps the suite fast without changing the path."""
    return dataclasses.replace(params, time_cost=1, memory_kib=8, parallelism=1)


def _init(passphrase: str):
    params, wrapped = vault.init_vault(passphrase)
    return _cheap(params), wrapped


def _reinit_cheap(passphrase: str):
    """init_vault uses default (expensive) params; rebuild the material at cheap
    cost so unlock() derives the same KEK the wrap used."""
    params = vault.KdfParams(salt=b"\x00" * 16, time_cost=1, memory_kib=8, parallelism=1)
    kek = params.derive(passphrase)
    dek = b"\x11" * 32
    wrapped = vault._wrap_dek(kek, dek)
    return params, wrapped, dek


def test_unlock_returns_the_same_dek_that_was_wrapped() -> None:
    params, wrapped, dek = _reinit_cheap("correct horse battery staple")
    assert vault.unlock("correct horse battery staple", params, wrapped) == dek


def test_a_wrong_passphrase_is_rejected() -> None:
    params, wrapped, _ = _reinit_cheap("the real passphrase")
    with pytest.raises(vault.WrongPassphrase):
        vault.unlock("not the passphrase", params, wrapped)


def test_a_roundtripped_secret_comes_back_intact() -> None:
    _, _, dek = _reinit_cheap("pw")
    nonce, ct = vault.encrypt_secret(dek, "anthropic", "sk-ant-secret-value")
    assert vault.decrypt_secret(dek, "anthropic", nonce, ct) == "sk-ant-secret-value"


def test_the_key_is_never_present_in_its_own_ciphertext() -> None:
    _, _, dek = _reinit_cheap("pw")
    _, ct = vault.encrypt_secret(dek, "anthropic", "sk-ant-PLAINTEXT-KEY")
    assert b"PLAINTEXT-KEY" not in ct


def test_a_secret_sealed_for_one_provider_will_not_open_as_another() -> None:
    """The provider name is authenticated AAD, so a ciphertext is bound to its
    row. Lifting the openai key into the anthropic slot must fail, not silently
    return the openai key under the wrong name."""
    _, _, dek = _reinit_cheap("pw")
    nonce, ct = vault.encrypt_secret(dek, "openai", "sk-openai")
    with pytest.raises(vault.VaultError):
        vault.decrypt_secret(dek, "anthropic", nonce, ct)


def test_a_tampered_ciphertext_is_rejected() -> None:
    _, _, dek = _reinit_cheap("pw")
    nonce, ct = vault.encrypt_secret(dek, "anthropic", "sk-ant")
    flipped = bytes([ct[0] ^ 0x01]) + ct[1:]
    with pytest.raises(vault.VaultError):
        vault.decrypt_secret(dek, "anthropic", nonce, flipped)


def test_a_wrong_dek_cannot_decrypt() -> None:
    _, _, dek = _reinit_cheap("pw")
    nonce, ct = vault.encrypt_secret(dek, "anthropic", "sk-ant")
    other_dek = bytes([b ^ 0xFF for b in dek])
    with pytest.raises(vault.VaultError):
        vault.decrypt_secret(other_dek, "anthropic", nonce, ct)


def test_two_encryptions_of_the_same_value_differ() -> None:
    """Fresh nonce per encrypt — identical inputs must not produce identical
    ciphertext, or an observer learns when two providers share a key."""
    _, _, dek = _reinit_cheap("pw")
    _, ct1 = vault.encrypt_secret(dek, "anthropic", "same")
    _, ct2 = vault.encrypt_secret(dek, "anthropic", "same")
    assert ct1 != ct2


def test_changing_the_passphrase_leaves_stored_secrets_readable() -> None:
    """The whole reason for the KEK/DEK split: a passphrase change re-wraps the
    DEK only. Secrets encrypted before the change decrypt unchanged after it."""
    params, wrapped, dek = _reinit_cheap("old passphrase")
    nonce, ct = vault.encrypt_secret(dek, "anthropic", "sk-ant-original")

    new_params, new_wrapped = vault.change_passphrase("old passphrase", "new passphrase", params, wrapped)

    dek_after = vault.unlock("new passphrase", new_params, new_wrapped)
    assert dek_after == dek
    assert vault.decrypt_secret(dek_after, "anthropic", nonce, ct) == "sk-ant-original"


def test_changing_the_passphrase_requires_the_old_one() -> None:
    params, wrapped, _ = _reinit_cheap("old passphrase")
    with pytest.raises(vault.WrongPassphrase):
        vault.change_passphrase("wrong old", "new", params, wrapped)


def test_the_old_passphrase_stops_working_after_a_change() -> None:
    params, wrapped, _ = _reinit_cheap("old passphrase")
    new_params, new_wrapped = vault.change_passphrase("old passphrase", "new passphrase", params, wrapped)
    with pytest.raises(vault.WrongPassphrase):
        vault.unlock("old passphrase", new_params, new_wrapped)


def test_init_produces_a_unique_salt_each_time() -> None:
    p1, _ = vault.init_vault("pw")
    p2, _ = vault.init_vault("pw")
    assert p1.salt != p2.salt, "salt reuse would make two setups derive the same KEK"
