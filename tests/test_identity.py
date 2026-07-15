"""Single-user identity, persistence, and session lifecycle.

The load-bearing guarantees, stated as tests:
  - a plaintext key is never obtainable without an unlocked session;
  - the DB on disk holds ciphertext, not keys;
  - a session expires, and an expired session cannot read secrets;
  - a passphrase change keeps every stored secret readable (the KEK/DEK split);
  - setup cannot be re-run over an existing vault (which would orphan secrets).

The vault's Argon2id default cost is too slow for a test suite, so we monkeypatch
init_vault/change_passphrase to a cheap cost. The crypto path is unchanged.
"""

from __future__ import annotations

import dataclasses
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app import identity as ident_mod  # noqa: E402
from app import vault  # noqa: E402
from app.identity import Identity, Locked, AlreadyInitialized, NotInitialized  # noqa: E402
from app.store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def _cheap_kdf(monkeypatch):
    """Make Argon2id negligible for the suite without touching the crypto path."""
    real_init = vault.init_vault
    real_change = vault.change_passphrase

    def cheap_init(passphrase):
        params, wrapped = real_init(passphrase)
        cheap = dataclasses.replace(params, time_cost=1, memory_kib=8, parallelism=1)
        # Re-wrap under the cheap-derived KEK so unlock() reproduces it.
        kek = cheap.derive(passphrase)
        return cheap, vault._wrap_dek(kek, vault.unlock(passphrase, params, wrapped))

    def cheap_change(old, new, params, wrapped):
        dek = vault.unlock(old, params, wrapped)
        new_params = dataclasses.replace(vault.init_vault(new)[0], time_cost=1, memory_kib=8, parallelism=1)
        return new_params, vault._wrap_dek(new_params.derive(new), dek)

    monkeypatch.setattr(ident_mod.vault, "init_vault", cheap_init)
    monkeypatch.setattr(ident_mod.vault, "change_passphrase", cheap_change)


def _identity(tmp_path: Path) -> Identity:
    return Identity(store=Store(tmp_path / "vault.db"))


def test_a_fresh_app_is_not_initialized(tmp_path: Path) -> None:
    assert _identity(tmp_path).is_initialized() is False


def test_initialize_sets_the_passphrase_and_returns_an_unlocked_session(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    token = ident.initialize("hunter2hunter2")
    assert ident.is_initialized() is True
    assert ident.is_unlocked(token) is True


def test_initialize_refuses_a_second_time(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    ident.initialize("hunter2hunter2")
    with pytest.raises(AlreadyInitialized):
        ident.initialize("another-one")


def test_initialize_rejects_a_short_passphrase(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _identity(tmp_path).initialize("short")


def test_unlock_with_the_right_passphrase_opens_a_session(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    ident.initialize("correct-passphrase")
    token = ident.unlock("correct-passphrase")
    assert ident.is_unlocked(token)


def test_unlock_with_the_wrong_passphrase_is_rejected(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    ident.initialize("correct-passphrase")
    with pytest.raises(vault.WrongPassphrase):
        ident.unlock("wrong-passphrase")


def test_unlock_before_setup_raises(tmp_path: Path) -> None:
    with pytest.raises(NotInitialized):
        _identity(tmp_path).unlock("anything")


def test_a_secret_cannot_be_read_without_a_session(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    token = ident.initialize("passphrase-1")
    ident.set_secret(token, "anthropic", "sk-ant-value")
    with pytest.raises(Locked):
        ident.get_secret("not-a-real-token", "anthropic")


def test_a_secret_roundtrips_through_an_unlocked_session(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    token = ident.initialize("passphrase-1")
    ident.set_secret(token, "anthropic", "sk-ant-value")
    assert ident.get_secret(token, "anthropic") == "sk-ant-value"


def test_the_database_file_holds_ciphertext_not_the_key(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    token = ident.initialize("passphrase-1")
    ident.set_secret(token, "anthropic", "sk-ant-SUPERSECRET")
    raw = (tmp_path / "vault.db").read_bytes()
    assert b"sk-ant-SUPERSECRET" not in raw
    assert b"SUPERSECRET" not in raw


def test_listing_secrets_reveals_presence_but_never_values(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    token = ident.initialize("passphrase-1")
    ident.set_secret(token, "anthropic", "sk-ant")
    ident.set_secret(token, "openai", "sk-oai")
    metas = ident.list_secrets()
    providers = {m.provider for m in metas}
    assert providers == {"anthropic", "openai"}
    # SecretMeta has no value field at all — presence and timestamp only.
    assert not any(hasattr(m, "value") for m in metas)


def test_providers_with_keys_needs_no_session(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    token = ident.initialize("passphrase-1")
    ident.set_secret(token, "anthropic", "sk-ant")
    assert ident.providers_with_keys() == {"anthropic"}


def test_deleting_a_secret_needs_no_session(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    token = ident.initialize("passphrase-1")
    ident.set_secret(token, "anthropic", "sk-ant")
    assert ident.delete_secret("anthropic") is True
    assert ident.providers_with_keys() == set()


def test_locking_a_session_stops_secret_access(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    token = ident.initialize("passphrase-1")
    ident.set_secret(token, "anthropic", "sk-ant")
    ident.lock(token)
    assert ident.is_unlocked(token) is False
    with pytest.raises(Locked):
        ident.get_secret(token, "anthropic")


def test_an_expired_session_cannot_read_secrets(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    token = ident.initialize("passphrase-1")
    ident.set_secret(token, "anthropic", "sk-ant")
    # Force this session past its deadline, the way wall-clock time eventually would.
    ident._sessions[token].expires_at = time.time() - 1
    assert ident.is_unlocked(token) is False
    with pytest.raises(Locked):
        ident.get_secret(token, "anthropic")


def test_secrets_survive_a_passphrase_change(tmp_path: Path) -> None:
    ident = _identity(tmp_path)
    token = ident.initialize("old-passphrase")
    ident.set_secret(token, "anthropic", "sk-ant-original")

    ident.change_passphrase("old-passphrase", "new-passphrase")

    # Old passphrase no longer unlocks; new one does; the secret is intact.
    with pytest.raises(vault.WrongPassphrase):
        ident.unlock("old-passphrase")
    new_token = ident.unlock("new-passphrase")
    assert ident.get_secret(new_token, "anthropic") == "sk-ant-original"


def test_secrets_persist_across_a_store_reopen(tmp_path: Path) -> None:
    """A restart drops sessions but not stored secrets — re-unlock reads them."""
    ident = _identity(tmp_path)
    token = ident.initialize("passphrase-1")
    ident.set_secret(token, "anthropic", "sk-ant-persisted")

    reopened = Identity(store=Store(tmp_path / "vault.db"))
    assert reopened.is_initialized()
    assert reopened.is_unlocked(token) is False, "sessions must not survive a restart"
    new_token = reopened.unlock("passphrase-1")
    assert reopened.get_secret(new_token, "anthropic") == "sk-ant-persisted"
