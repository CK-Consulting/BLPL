"""SQLite persistence for vault material and encrypted secrets.

This is the *only* thing in the system that must survive a restart and must not
be readable in the clear. It holds two kinds of row:

  - the vault material: the KDF salt/params and the wrapped DEK, in a single
    ``vault`` row. Not secret on its own — useless without the passphrase.
  - one ``secret`` row per provider: the encrypted API key, its nonce, and the
    provider name (which is also the AES-GCM AAD, so it is authenticated).

Non-secret configuration deliberately does NOT live here. The user asked for a
nix-like split: declarative plaintext config in ``blpl.toml`` (which provider to
prefer, which model), secrets in the encrypted DB. Keeping them apart means the
config is diffable and reviewable and the DB is opaque.

The DEK never touches this module. Rows go in and out encrypted; decryption
happens in the session layer, which holds the DEK in RAM. So a dump of this file
is a dump of ciphertext.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import vault

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault (
    id           INTEGER PRIMARY KEY CHECK (id = 1),  -- single-user: exactly one row
    salt         BLOB NOT NULL,
    time_cost    INTEGER NOT NULL,
    memory_kib   INTEGER NOT NULL,
    parallelism  INTEGER NOT NULL,
    dek_nonce    BLOB NOT NULL,
    dek_wrapped  BLOB NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS secret (
    provider    TEXT PRIMARY KEY,
    nonce       BLOB NOT NULL,
    ciphertext  BLOB NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The same DEK as the vault row, wrapped under the server key instead of the
-- passphrase, so an SSO login can open a session. Separate table rather than
-- two more columns on `vault`: its presence is exactly the question "is SSO
-- enabled", and DELETE answers it in one statement without disturbing the
-- passphrase material sitting next to it.
CREATE TABLE IF NOT EXISTS vault_server_key (
    id           INTEGER PRIMARY KEY CHECK (id = 1),  -- single-user: exactly one row
    dek_nonce    BLOB NOT NULL,
    dek_wrapped  BLOB NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@dataclass(frozen=True)
class SecretMeta:
    """What is safe to tell a UI about a stored secret: that it exists, and when
    it changed. Never the value."""

    provider: str
    updated_at: str


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- vault material ------------------------------------------------------

    def is_initialized(self) -> bool:
        row = self._conn.execute("SELECT 1 FROM vault WHERE id = 1").fetchone()
        return row is not None

    def save_vault(self, params: vault.KdfParams, wrapped: vault.WrappedDek) -> None:
        """Persist (or replace) the single vault row. Replacing it is how a
        passphrase change lands — the DEK is the same, its wrapping is new."""
        self._conn.execute(
            """
            INSERT INTO vault (id, salt, time_cost, memory_kib, parallelism, dek_nonce, dek_wrapped)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                salt=excluded.salt, time_cost=excluded.time_cost,
                memory_kib=excluded.memory_kib, parallelism=excluded.parallelism,
                dek_nonce=excluded.dek_nonce, dek_wrapped=excluded.dek_wrapped
            """,
            (
                params.salt,
                params.time_cost,
                params.memory_kib,
                params.parallelism,
                wrapped.nonce,
                wrapped.ciphertext,
            ),
        )
        self._conn.commit()

    def load_vault(self) -> tuple[vault.KdfParams, vault.WrappedDek]:
        row = self._conn.execute("SELECT * FROM vault WHERE id = 1").fetchone()
        if row is None:
            raise LookupError("vault is not initialized")
        params = vault.KdfParams(
            salt=row["salt"],
            time_cost=row["time_cost"],
            memory_kib=row["memory_kib"],
            parallelism=row["parallelism"],
        )
        wrapped = vault.WrappedDek(nonce=row["dek_nonce"], ciphertext=row["dek_wrapped"])
        return params, wrapped

    # -- server-key wrapping (SSO login) -------------------------------------

    def has_server_wrapped_dek(self) -> bool:
        row = self._conn.execute("SELECT 1 FROM vault_server_key WHERE id = 1").fetchone()
        return row is not None

    def save_server_wrapped_dek(self, wrapped: vault.WrappedDek) -> None:
        self._conn.execute(
            """
            INSERT INTO vault_server_key (id, dek_nonce, dek_wrapped)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                dek_nonce=excluded.dek_nonce, dek_wrapped=excluded.dek_wrapped
            """,
            (wrapped.nonce, wrapped.ciphertext),
        )
        self._conn.commit()

    def load_server_wrapped_dek(self) -> vault.WrappedDek | None:
        row = self._conn.execute("SELECT * FROM vault_server_key WHERE id = 1").fetchone()
        if row is None:
            return None
        return vault.WrappedDek(nonce=row["dek_nonce"], ciphertext=row["dek_wrapped"])

    def delete_server_wrapped_dek(self) -> bool:
        """Turn SSO login back off. Returns whether there was anything to delete.

        Only the second wrapping goes; the passphrase wrapping and every secret
        are untouched, so this is a safe thing to reach for in a hurry — which
        is the point, because "the server key may have leaked" is the situation
        it exists for.
        """
        cur = self._conn.execute("DELETE FROM vault_server_key WHERE id = 1")
        self._conn.commit()
        return cur.rowcount > 0

    # -- secrets -------------------------------------------------------------

    def put_secret(self, provider: str, nonce: bytes, ciphertext: bytes) -> None:
        self._conn.execute(
            """
            INSERT INTO secret (provider, nonce, ciphertext, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(provider) DO UPDATE SET
                nonce=excluded.nonce, ciphertext=excluded.ciphertext,
                updated_at=datetime('now')
            """,
            (provider, nonce, ciphertext),
        )
        self._conn.commit()

    def get_secret_blob(self, provider: str) -> tuple[bytes, bytes] | None:
        row = self._conn.execute(
            "SELECT nonce, ciphertext FROM secret WHERE provider = ?", (provider,)
        ).fetchone()
        if row is None:
            return None
        return row["nonce"], row["ciphertext"]

    def delete_secret(self, provider: str) -> bool:
        cur = self._conn.execute("DELETE FROM secret WHERE provider = ?", (provider,))
        self._conn.commit()
        return cur.rowcount > 0

    def list_secret_meta(self) -> list[SecretMeta]:
        """Provider names and timestamps only — never ciphertext, never values.
        This is what a settings page is allowed to see without unlocking."""
        rows = self._conn.execute(
            "SELECT provider, updated_at FROM secret ORDER BY provider"
        ).fetchall()
        return [SecretMeta(provider=r["provider"], updated_at=r["updated_at"]) for r in rows]

    def close(self) -> None:
        self._conn.close()
