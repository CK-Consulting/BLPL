"""Write-through persistence for the open-workspace registry.

app/workspace.py's ``Registry`` is a process-local dict and stays one — it is
on the hot path, it is well covered, and making it talk to a database would put
a query behind every file access. This module is the other half: the same facts,
written through to ``open_workspace`` so that a restart can pick them up.

The reason it exists at all is the crash case. Locking a session seals the
project; the idle sweeper seals the project of someone who walked away. Both
read the dict. If the process dies while a project is open, the dict dies with
it and **nothing** seals that project — it stays plaintext on disk until
somebody happens to open and lock it again. That is not a small window and it is
not bounded by anything.

Restoring needs the project key, which is the part that makes this more than a
bookkeeping table: the key normally comes from a user's master key, and after a
restart there is no user. So the key is wrapped under the server key while the
workspace is open, and unwrapped at boot. models.OpenWorkspaceRow sets out why
that is not a new exposure — the row lives exactly as long as the plaintext
directory it describes.

Every function here is best-effort with respect to the *caller*: a database
hiccup must not stop someone opening a project, and must not turn a successful
seal into an error. What it must never do is silently lose a row, so failures
are logged rather than swallowed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import OpenWorkspaceRow

logger = logging.getLogger("blpl.app")

_NONCE_LEN = 12
_AAD_PREFIX = b"blpl-open-workspace:"

#: How stale a persisted ``last_touched`` may get. The registry is touched on
#: every file access; the database is not. Sealing waits for IDLE_AFTER (30
#: minutes), so a minute of drift costs nothing and saves a write per request.
TOUCH_THROTTLE = timedelta(minutes=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aad(workspace: str) -> bytes:
    """Bind a wrapped key to its workspace, so a row cannot be replayed."""
    return _AAD_PREFIX + workspace.encode("utf-8")


def wrap(server_key: bytes, workspace: str, project_key: bytes) -> tuple[bytes, bytes]:
    """Seal a project key under the server key. Returns (nonce, ciphertext)."""
    import os

    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(server_key).encrypt(nonce, project_key, _aad(workspace))
    return nonce, ct


def unwrap(server_key: bytes, workspace: str, nonce: bytes, ciphertext: bytes) -> bytes:
    """Recover a project key. Raises InvalidTag if the key or row is wrong."""
    return AESGCM(server_key).decrypt(nonce, ciphertext, _aad(workspace))


def record_open(
    session: Session,
    workspace: str,
    path: Path,
    project_key: bytes,
    server_key: bytes,
    holder: int | None = None,
) -> None:
    """Note a workspace as open, or add a holder to one already recorded.

    Mirrors ``Registry.note_open``: re-opening does not replace the row, because
    the holders already in it are still in there and dropping them would make
    the next lock seal the project out from under them.
    """
    row = session.get(OpenWorkspaceRow, workspace)
    if row is None:
        nonce, ct = wrap(server_key, workspace, project_key)
        row = OpenWorkspaceRow(
            workspace=workspace,
            path=str(path),
            nonce=nonce,
            wrapped_key=ct,
            holders=[],
            last_touched=_now(),
        )
        session.add(row)
    row.last_touched = _now()
    if holder is not None and holder not in (row.holders or []):
        # Reassigned rather than appended: SQLAlchemy does not track mutation
        # inside a JSON list, and an in-place append would never be written.
        row.holders = [*(row.holders or []), holder]


def touch(session: Session, workspace: str, holder: int | None = None) -> None:
    """Note activity, at most once per TOUCH_THROTTLE unless holders change."""
    row = session.get(OpenWorkspaceRow, workspace)
    if row is None:
        return
    new_holder = holder is not None and holder not in (row.holders or [])
    if new_holder:
        row.holders = [*(row.holders or []), holder]
    last = row.last_touched
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if new_holder or last is None or _now() - last >= TOUCH_THROTTLE:
        row.last_touched = _now()


def release(session: Session, workspace: str, holder: int) -> None:
    """One person is done. Whether that means sealing is the registry's call."""
    row = session.get(OpenWorkspaceRow, workspace)
    if row is None:
        return
    row.holders = [h for h in (row.holders or []) if h != holder]


def forget(session: Session, workspace: str) -> None:
    """The workspace is sealed. Drop the row, and with it the wrapped key."""
    session.execute(delete(OpenWorkspaceRow).where(OpenWorkspaceRow.workspace == workspace))


def restore(session: Session, server_key: bytes, projects_root: Path) -> list[tuple[str, Path, bytes, datetime, set[int]]]:
    """Every workspace that was open when the process last stopped.

    Rows whose directory is gone are dropped: the workspace was sealed, or
    removed, by something that never got to clear the row, and keeping it would
    have the sweeper try to seal a path that is not there.

    A row whose key will not unwrap is *kept* and reported as an error. It means
    the server key changed — restoring from a backup with the wrong key file, or
    losing it — and in that state the unsealed directory cannot be re-sealed by
    anyone. Deleting the row would hide a project sitting in plaintext, which is
    the one outcome this module exists to prevent.
    """
    out: list[tuple[str, Path, bytes, datetime, set[int]]] = []
    for row in session.scalars(select(OpenWorkspaceRow)).all():
        path = Path(row.path)
        if not path.is_dir():
            logger.info("open workspace %s has no directory; dropping the row", row.workspace)
            session.delete(row)
            continue
        try:
            key = unwrap(server_key, row.workspace, row.nonce, row.wrapped_key)
        except InvalidTag:
            logger.error(
                "cannot unwrap the key for open workspace %s — the server key does not match. "
                "%s is UNSEALED on disk and nothing can re-seal it until a member opens it "
                "with their own key.",
                row.workspace,
                path,
            )
            continue
        touched = row.last_touched or _now()
        if touched.tzinfo is None:
            touched = touched.replace(tzinfo=timezone.utc)
        out.append((row.workspace, path, key, touched, set(row.holders or [])))
    return out
