"""The key that lets an SSO login open the vault without a passphrase.

An OAuth login proves *who you are*. It produces no key material, so by itself
it cannot decrypt anything. This module supplies the missing half: 32 bytes the
server holds, under which app/vault.py keeps a second wrapping of the same DEK.
Sign in with GitLab, the server unwraps the DEK with this key, and the session is
open — no passphrase in the loop.

That is a real reduction in what the encryption protects against, stated plainly
here because it is easy to forget once it works: ``vault.db`` plus this key is
every secret you own. Before, a stolen disk image was inert without a passphrase
that existed only in your head. So:

* The key lives *outside* the database it opens. Keeping both in one file would
  make the encryption ornamental — a single stolen file would carry the lock and
  its key together.
* On disk it is written 0600 and re-checked on every read, because a key file
  the whole host can read is not a key.
* ``BLPL_SERVER_KEY`` takes precedence, so a deploy that keeps secrets in a
  secret manager (or a tmpfs mount) never has to materialise the file at all.

No key configured and no file on disk means SSO is simply off. Nothing is
auto-generated at import time: the file appears only when something explicitly
asks to enable OAuth login, so a passphrase-only install stays passphrase-only
and its threat model stays intact.
"""

from __future__ import annotations

import base64
import binascii
import os
import stat
from pathlib import Path

from .vault import VaultError

_KEY_LEN = 32
_ENV_VAR = "BLPL_SERVER_KEY"
_FILE_MODE = 0o600


class ServerKeyError(VaultError):
    """The configured server key is unusable. Never raised for a *missing* key —
    absence means "SSO is off", which is a valid state, not a failure."""


def encode(key: bytes) -> str:
    """The wire/env form: urlsafe base64, no padding. What to paste into
    BLPL_SERVER_KEY."""
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")


def generate() -> bytes:
    return os.urandom(_KEY_LEN)


def load(data_root: Path) -> bytes | None:
    """The configured server key, or None if SSO has never been enabled.

    Environment first: a deploy that injects the key has said something more
    deliberate than a file left over from an earlier run, and it is the form that
    lets the key live in a secret manager instead of on the volume.
    """
    from_env = os.environ.get(_ENV_VAR, "").strip()
    if from_env:
        return _decode(from_env)
    path = key_path(data_root)
    if not path.exists():
        return None
    _require_private(path)
    return _decode(path.read_text(encoding="utf-8").strip())


def load_or_create(data_root: Path) -> bytes:
    """The server key, generating and persisting one if there is none.

    Only called from the enable-OAuth path. A read that happens to find no key
    must not quietly mint one — that would turn "SSO is off" into "SSO is on"
    as a side effect of asking a question.
    """
    existing = load(data_root)
    if existing is not None:
        return existing
    key = generate()
    path = key_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with the mode already restrictive rather than chmod-ing after: an
    # 0644 window between write and chmod is enough for another process on the
    # host to read the key, and nothing about that window is recoverable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(encode(key) + "\n")
    return key


def key_path(data_root: Path) -> Path:
    return Path(data_root) / "server.key"


def _decode(value: str) -> bytes:
    """Accept base64 (with or without padding) or hex, because an operator
    generating one by hand will reach for either."""
    raw: bytes | None = None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except (binascii.Error, ValueError):
        raw = None
    if raw is None or len(raw) != _KEY_LEN:
        try:
            candidate = bytes.fromhex(value)
        except ValueError:
            candidate = b""
        if len(candidate) == _KEY_LEN:
            raw = candidate
    if raw is None or len(raw) != _KEY_LEN:
        raise ServerKeyError(
            f"{_ENV_VAR} must decode to {_KEY_LEN} bytes (base64 or hex); "
            "generate one with: python -c \"import os,base64; "
            "print(base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode())\""
        )
    return raw


def _require_private(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ServerKeyError(
            f"{path} is mode {mode:o}; it unwraps the vault and must not be readable "
            f"by group or other. Fix with: chmod 600 {path}"
        )
