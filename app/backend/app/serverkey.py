"""The key that seals every stored provider key.

Clerk establishes who a user is and holds nothing that could decrypt their data,
and since a user never hands us a secret there is nothing to derive a per-user
key from. So provider keys in Postgres are sealed under one key the server holds:
these 32 bytes.

That is a real limit, stated plainly because it is easy to forget once it works:
the database plus this key is every user's keys, and the operator has both. What
it does buy is that a dump, a snapshot, or a stolen backup is inert on its own.
So:

* The key lives *outside* the database it opens. In one file they would be lock
  and key together, and the encryption would be decoration.
* On disk it is written 0600 and re-checked on every read, because a key file the
  whole host can read is not a key.
* ``BLPL_SERVER_KEY`` takes precedence, so a deploy keeping secrets in a secret
  manager or on a tmpfs never has to materialise the file at all.

Losing it is not recoverable: every stored key becomes ciphertext nobody can
open, and every user has to re-enter theirs. It belongs in a backup, and not the
same one as the database.
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
    """The configured server key is unusable — malformed, or a key file the whole
    host can read. Never raised for a *missing* key: absence is answered by
    creating one at startup, not by failing."""


def encode(key: bytes) -> str:
    """The wire/env form: urlsafe base64, no padding. What to paste into
    BLPL_SERVER_KEY."""
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")


def generate() -> bytes:
    return os.urandom(_KEY_LEN)


def load(data_root: Path) -> bytes | None:
    """The configured server key, or None if there is not one yet.

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

    Called once at startup, deliberately not lazily from the first write: the
    operator should learn the key exists — and get it into a backup — before it
    becomes the only thing between a database dump and every user's keys.
    ``load`` stays non-creating so that merely *asking* whether a key exists
    never brings one into being.
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
            f"{path} is mode {mode:o}; it opens every stored provider key and must "
            f"not be readable by group or other. Fix with: chmod 600 {path}"
        )
