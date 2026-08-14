"""Single-user identity: set a passphrase, unlock a session, use the secrets.

This is the layer that holds the DEK. The rule it enforces is simple and total:
the decrypted DEK exists only in this process's memory, only inside an unlocked
session, and never anywhere else. It is not written to disk, not put in a JWT,
not returned over the wire. A server restart drops every session, and unlocking
again is the only way back in — which is the behaviour you want for a box you
leave running and roam back to.

There are two ways to reach an unlocked session, and they differ in what they
prove. The passphrase *derives* the key — knowing it is the same as holding the
key. An SSO login proves only identity, so it opens a session via a second
wrapping of the same DEK held under a server key (app/serverkey.py); the vault
module's docstring spells out what that costs. Either way what lands here is the
same DEK in the same in-memory session, and everything above this layer is
unable to tell which door was used.

Sessions are in-process because the backend runs a single worker on purpose (SSE
stage streams come off an in-process subprocess, so multiple workers would break
them anyway). A token is handed to the browser as an httpOnly cookie; the token
maps to a DEK held here. Tokens are opaque random strings — losing the cookie
loses access to the session, not to the keys, because the keys are useless
without the DEK the token points at, and the DEK is gone the moment the process
stops.
"""

from __future__ import annotations

import os
import secrets as _stdlib_secrets
import time
from dataclasses import dataclass, field

from . import vault
from .store import SecretMeta, Store

# How long an unlocked session lasts without use. Long enough not to nag across a
# work session; short enough that a walked-away-from browser re-locks on its own.
_SESSION_TTL_SECONDS = 8 * 3600


class NotInitialized(RuntimeError):
    """No passphrase has been set yet — the app needs first-run setup."""


class AlreadyInitialized(RuntimeError):
    """Setup was attempted on a vault that already exists. Refused, because it
    would orphan every secret encrypted under the current DEK."""


class Locked(RuntimeError):
    """An operation needed the DEK but no unlocked session was supplied."""


class SsoNotEnabled(RuntimeError):
    """An SSO login succeeded, but this vault has no server-key wrapping to open.

    The remedy is specific and worth keeping distinct from a failed login: unlock
    once with the passphrase while OAuth is configured, which enrols the wrapping.
    Until then the identity is fine and the vault is simply shut to it.
    """


@dataclass
class _Session:
    dek: bytes
    expires_at: float


@dataclass
class Identity:
    """The app's single-user identity and secret service.

    Wraps a Store (persistence) and the vault (crypto), and owns the live
    sessions. Everything above this layer speaks in provider names and session
    tokens; only this layer ever sees a plaintext key or the DEK.
    """

    store: Store
    _sessions: dict[str, _Session] = field(default_factory=dict)

    # -- setup / unlock ------------------------------------------------------

    def is_initialized(self) -> bool:
        return self.store.is_initialized()

    def initialize(self, passphrase: str) -> str:
        """First-run: set the passphrase, and return a token for an already-unlocked
        session so the user isn't asked to type it twice in a row."""
        if self.store.is_initialized():
            raise AlreadyInitialized("a passphrase is already set")
        if len(passphrase) < 8:
            raise ValueError("passphrase must be at least 8 characters")
        params, wrapped = vault.init_vault(passphrase)
        self.store.save_vault(params, wrapped)
        dek = vault.unlock(passphrase, params, wrapped)
        return self._open_session(dek)

    def unlock(self, passphrase: str) -> str:
        """Verify the passphrase and open a session, returning its token.
        Raises vault.WrongPassphrase on a bad passphrase."""
        if not self.store.is_initialized():
            raise NotInitialized("no passphrase has been set")
        params, wrapped = self.store.load_vault()
        dek = vault.unlock(passphrase, params, wrapped)  # raises WrongPassphrase
        return self._open_session(dek)

    # -- SSO login -----------------------------------------------------------

    def sso_enabled(self) -> bool:
        """Whether a server-key wrapping exists, i.e. whether an OAuth login can
        open a session. Cheap and session-free, because the unlock screen has to
        ask this before anyone is authenticated."""
        return self.store.has_server_wrapped_dek()

    def enable_sso(self, token: str, server_key: bytes) -> None:
        """Wrap the live DEK under the server key so SSO logins can unlock.

        Requires an unlocked session by construction: the DEK it wraps is the one
        held by ``token``. You cannot enable SSO without first proving you can
        already open the vault, which is what stops a stolen server key from
        being enough on its own to enrol.

        Idempotent — re-enrolling rewraps the same DEK under the same key, which
        is how a rotated server key is adopted.
        """
        dek = self._require_dek(token)
        self.store.save_server_wrapped_dek(vault.wrap_dek_with_key(server_key, dek))

    def disable_sso(self) -> bool:
        """Drop the server-key wrapping. Needs no session: revoking access is
        never something to gate behind having access. Existing sessions live on
        — they already hold the DEK — but no new SSO login can open one."""
        return self.store.delete_server_wrapped_dek()

    def unlock_with_server_key(self, server_key: bytes) -> str:
        """Open a session from the server-key wrapping, returning its token.

        The caller must have established *identity* before calling this: this
        function checks a key the server holds, not anything about the person.
        It is the last step of an OAuth callback, never a route of its own.
        """
        if not self.store.is_initialized():
            raise NotInitialized("no passphrase has been set")
        wrapped = self.store.load_server_wrapped_dek()
        if wrapped is None:
            raise SsoNotEnabled("no server-key wrapping; sign in with the passphrase once first")
        dek = vault.unwrap_dek_with_key(server_key, wrapped)  # raises WrongServerKey
        return self._open_session(dek)

    def change_passphrase(self, old: str, new: str) -> None:
        if not self.store.is_initialized():
            raise NotInitialized("no passphrase has been set")
        if len(new) < 8:
            raise ValueError("passphrase must be at least 8 characters")
        params, wrapped = self.store.load_vault()
        new_params, new_wrapped = vault.change_passphrase(old, new, params, wrapped)
        self.store.save_vault(new_params, new_wrapped)
        # Existing sessions still hold the same DEK (it didn't change), so they
        # stay valid. Nothing to invalidate.

    def lock(self, token: str) -> None:
        """Drop a session. Idempotent — locking an unknown token is a no-op."""
        self._sessions.pop(token, None)

    def is_unlocked(self, token: str | None) -> bool:
        return token is not None and self._live_session(token) is not None

    # -- secrets (require an unlocked session) -------------------------------

    def set_secret(self, token: str, provider: str, value: str) -> None:
        dek = self._require_dek(token)
        provider = _normalize_provider(provider)
        nonce, ciphertext = vault.encrypt_secret(dek, provider, value)
        self.store.put_secret(provider, nonce, ciphertext)

    def get_secret(self, token: str, provider: str) -> str | None:
        """The plaintext key. The ONLY caller is the stage runner injecting it
        into a subprocess env — never an HTTP response body."""
        dek = self._require_dek(token)
        provider = _normalize_provider(provider)
        blob = self.store.get_secret_blob(provider)
        if blob is None:
            return None
        nonce, ciphertext = blob
        return vault.decrypt_secret(dek, provider, nonce, ciphertext)

    def delete_secret(self, provider: str) -> bool:
        # Deleting ciphertext needs no DEK — you can forget a key you can't read.
        return self.store.delete_secret(_normalize_provider(provider))

    def list_secrets(self) -> list[SecretMeta]:
        """Presence and timestamps, safe without a session. Never values."""
        return self.store.list_secret_meta()

    def providers_with_keys(self) -> set[str]:
        return {m.provider for m in self.store.list_secret_meta()}

    # -- session internals ---------------------------------------------------

    def _open_session(self, dek: bytes) -> str:
        token = _stdlib_secrets.token_urlsafe(32)
        self._sessions[token] = _Session(dek=dek, expires_at=time.time() + _SESSION_TTL_SECONDS)
        return token

    def _live_session(self, token: str) -> _Session | None:
        sess = self._sessions.get(token)
        if sess is None:
            return None
        if sess.expires_at < time.time():
            self._sessions.pop(token, None)
            return None
        # Sliding expiry: activity keeps a session alive.
        sess.expires_at = time.time() + _SESSION_TTL_SECONDS
        return sess

    def _require_dek(self, token: str | None) -> bytes:
        if token is None:
            raise Locked("no session")
        sess = self._live_session(token)
        if sess is None:
            raise Locked("session is locked or expired")
        return sess.dek


def _normalize_provider(provider: str) -> str:
    p = provider.strip().lower()
    if not p or not p.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"invalid provider name {provider!r}")
    return p
