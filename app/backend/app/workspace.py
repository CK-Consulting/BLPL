"""Project files, sealed when nobody is using them.

The constraint that shaped this: **diffing must not break**. KiCad's files are
text, which is the reason the whole pipeline can be parametric and deterministic,
and that only holds while git can read them. So per-file encryption is out — a
git-crypt-style filter turns every `.kicad_sch` diff into binary noise and takes
the main reason for choosing KiCad with it.

What is left is sealing at the storage boundary. The entire project, `.git`
included, becomes one encrypted blob when idle and is restored to an ordinary
directory when opened. Git only ever sees plaintext, so diff, blame and merge are
untouched — they are not aware any of this happened.

    projects/baseboard/              the repository and the owner's checkout
    projects/.worktrees/baseboard/   every member's checkout
    projects/.sealed/baseboard.blob  both of the above, sealed, when nobody is in

The unit is the **project and all its worktrees together**, and that is forced
rather than chosen. A git worktree is not a copy: it holds a `.git` *file*
pointing back into the repository's object store. Sealing the project directory
on its own would delete that store and break every member's checkout, and sealing
a member's worktree on its own would leave the repository — which contains every
version of everything they have committed — sitting in plaintext beside it.

So a project is open when anyone has it open, and sealed once everybody has
finished. Per-member sealing sounds stronger and is not available: while any
member has it open, the shared object store is plaintext on disk regardless.

Be exact about what this protects, because it is easy to over-read:

* A stolen disk, backup or snapshot of a project nobody had open: **inert**.
* A project that is open: plaintext on disk and its key in this process's
  memory. Sealing is not a sandbox.
* The operator of a running server: unchanged. They can read an open project.

And one thing it deliberately does *not* do: seal to tmpfs. Plaintext in RAM
would vanish on restart, which sounds better until it takes uncommitted work with
it. A workspace left open by a crash stays on disk and is sealed the next time
its owner unlocks — a window measured in however long they are away, and a
window is better than losing an afternoon.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .vault import VaultError

SEALED_DIR = ".sealed"
_NONCE_LEN = 12
_AAD = b"blpl-workspace"

# How long an open workspace may sit untouched before it is sealed again. Long
# enough to read a datasheet and come back; short enough that a forgotten tab
# does not leave a project readable all night.
IDLE_AFTER = timedelta(minutes=30)


class WorkspaceError(VaultError):
    """A workspace could not be sealed or opened."""


@dataclass
class OpenWorkspace:
    """An unsealed project, who is in it, and the key that will seal it again.

    The key is held here rather than looked up when sealing, and that is the
    whole reason idle sealing can work at all: by the time a workspace has gone
    idle its owner may be long gone, and their master key with them. Keeping it
    is no additional exposure — the workspace it opens is already plaintext on
    disk beside it.

    ``holders`` is what makes locking safe on a shared project. One project has
    one key and one set of files, so a member locking their session must not
    seal a workspace a colleague is still working in — the two of them are in
    the same directory tree, and sealing it would delete the files out from
    under them.
    """

    workspace: str
    path: Path
    key: bytes
    last_touched: datetime
    holders: set[int] = field(default_factory=set)


class Registry:
    """Which workspaces are open, who is in them, and what will re-seal them."""

    def __init__(self) -> None:
        self._open: dict[str, OpenWorkspace] = {}

    def note_open(self, workspace: str, path: Path, key: bytes, holder: int | None = None) -> None:
        """Record a workspace as open, or join one that already is.

        Re-opening does not replace the entry: the existing holders are still in
        there, and dropping them would make the next lock seal the project out
        from under them.
        """
        entry = self._open.get(workspace)
        if entry is None:
            entry = OpenWorkspace(workspace, path, key, _now())
            self._open[workspace] = entry
        entry.last_touched = _now()
        if holder is not None:
            entry.holders.add(holder)

    def adopt(
        self,
        workspace: str,
        path: Path,
        key: bytes,
        last_touched: datetime,
        holders: set[int],
    ) -> None:
        """Put back an entry that outlived the process that made it.

        Separate from ``note_open`` because the timestamp must be the one the
        workspace actually had, not now: a project left open by a crash three
        hours ago is already overdue for sealing, and restoring it as freshly
        touched would grant it another full idle window on every restart — a
        crash loop would keep it plaintext forever.
        """
        entry = OpenWorkspace(workspace, path, key, last_touched)
        entry.holders = set(holders)
        self._open[workspace] = entry

    def touch(self, workspace: str, holder: int | None = None) -> None:
        """Note activity. Reaching a project's files counts as being in it —
        which is how someone who never called open still holds it open, and how
        their lock is what lets it go."""
        entry = self._open.get(workspace)
        if entry is not None:
            entry.last_touched = _now()
            if holder is not None:
                entry.holders.add(holder)

    def release(self, workspace: str, holder: int) -> bool:
        """One person is done. True when nobody is left and it may be sealed."""
        entry = self._open.get(workspace)
        if entry is None:
            return False
        entry.holders.discard(holder)
        return not entry.holders

    def is_open(self, workspace: str) -> bool:
        return workspace in self._open

    def holders_of(self, workspace: str) -> set[int]:
        entry = self._open.get(workspace)
        return set(entry.holders) if entry else set()

    def key_for(self, workspace: str) -> bytes | None:
        entry = self._open.get(workspace)
        return entry.key if entry else None

    def forget(self, workspace: str) -> None:
        self._open.pop(workspace, None)

    def idle(self, cutoff: timedelta = IDLE_AFTER) -> list[OpenWorkspace]:
        """Workspaces nobody has touched lately, holders or not.

        Deliberately ignores holders: someone who walked away without locking is
        precisely the case this exists for, and waiting for them to say they are
        done would mean waiting forever.
        """
        limit = _now() - cutoff
        return [w for w in self._open.values() if w.last_touched < limit]

    def all_open(self) -> list[OpenWorkspace]:
        return list(self._open.values())


def sealed_path(projects_root: Path, workspace: str) -> Path:
    return Path(projects_root) / SEALED_DIR / f"{workspace}.blob"


def is_sealed(projects_root: Path, workspace: str) -> bool:
    return sealed_path(projects_root, workspace).exists()


def parts_of(projects_root: Path, project: str) -> list[tuple[str, Path]]:
    """The directories that make up one project: the repository, and the
    worktrees that point into it. Sealed and restored as a unit because git
    makes them one."""
    root = Path(projects_root)
    parts = [("repo", root / project)]
    worktrees_dir = root / ".worktrees" / project
    if worktrees_dir.is_dir():
        parts.append(("worktrees", worktrees_dir))
    return parts


@contextlib.contextmanager
def _project_lock(projects_root: Path, project: str):
    """Hold one project's seal/unseal lock.

    Opening a project was not serialised, and the frontend can ask for it
    several times at once — six `POST /open` in 75 ms, on one occasion. Every
    one of them unsealed into the same staging directory: the later ones
    `rmtree`d it while the first was mid-extract, four crashed on "Directory not
    empty", and a survivor moved the half-deleted tree into place and removed
    the blob. The project came back with its design files and without `HEAD`,
    `config` or its branch ref, so git no longer recognised it as a repository
    and every commit after that failed.

    `flock` on a file beside the blobs, so the lock holds across the request
    threads of one process and across the containers that share the bind
    mount. Every call opens its own descriptor, which is what makes two
    threads of the same process exclude each other.
    """
    lock_dir = Path(projects_root) / SEALED_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / f".{project}.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def seal(projects_root: Path, project: str, key: bytes) -> int:
    with _project_lock(projects_root, project):
        return _seal(projects_root, project, key)


def _seal(projects_root: Path, project: str, key: bytes) -> int:
    """Archive the project and its worktrees, encrypt, and remove the plaintext.

    Written to a temporary file and moved into place, so an interrupted seal
    leaves either the old blob or the new one — never a truncated blob beside a
    directory that has already been deleted, which would be the one failure that
    loses a project outright.

    Returns the sealed size, for the log.
    """
    root = Path(projects_root)
    parts = [(name, path) for name, path in parts_of(root, project) if path.is_dir()]
    if not parts:
        raise WorkspaceError(f"no working copy to seal for {project!r}")

    buf = io.BytesIO()
    # Uncompressed: KiCad files are text and compress well, but these blobs are
    # written on every idle sweep and read on every open, and gzip on a large
    # project is slower than the disk it saves.
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, path in parts:
            tar.add(path, arcname=name)
    plaintext = buf.getvalue()

    nonce = os.urandom(_NONCE_LEN)
    blob = nonce + AESGCM(key).encrypt(nonce, plaintext, _AAD)

    target = sealed_path(root, project)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".blob.tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, target)

    for _name, path in parts:
        shutil.rmtree(path)
    return len(blob)


def unseal(projects_root: Path, project: str, key: bytes) -> Path:
    """Restore a project and its worktrees from their blob.

    Extract first, then remove the blob — never the other way round. Dying
    between the two leaves both copies, and the next open finds the directory
    already there and uses it; doing it in the other order and dying leaves
    neither, which loses the project.

    Serialised per project (see `_project_lock`). A caller that waited on the
    lock behind another unseal of the same project finds the directory restored
    and the blob gone, and returns the directory rather than failing.
    """
    with _project_lock(projects_root, project):
        return _unseal(projects_root, project, key)


def _unseal(projects_root: Path, project: str, key: bytes) -> Path:
    root = Path(projects_root)
    repo = root / project
    if repo.exists() and not sealed_path(root, project).exists():
        return repo
    blob_path = sealed_path(root, project)
    if not blob_path.exists():
        raise WorkspaceError(f"nothing sealed at {blob_path}")

    blob = blob_path.read_bytes()
    nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _AAD)
    except InvalidTag as exc:
        raise WorkspaceError(
            f"could not open {project!r} — this is not the key it was sealed with"
        ) from exc

    if repo.exists():
        # Already open. Extracting over it would replace what is there with an
        # older copy, which is the shape of an accidental data loss.
        return repo

    # A directory of this call's own. The lock already keeps two restores of
    # one project apart; a private staging directory means that even without
    # it, no call can delete files another is extracting. A leftover from a
    # restore that died is ignored rather than cleaned up here.
    staging = Path(tempfile.mkdtemp(prefix=f".{project}.restore-", dir=root / SEALED_DIR))
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r") as tar:
        # filter="data" refuses absolute paths, links escaping the destination
        # and device nodes. The archive is one we wrote, but it arrives from
        # disk and is decrypted with a key several people hold, so it is treated
        # as input rather than trusted.
        tar.extractall(staging, filter="data")

    # Into place only once everything has extracted. A half-restored project
    # would look real to every listing and to git.
    (root / ".worktrees").mkdir(parents=True, exist_ok=True)
    os.replace(staging / "repo", repo)
    if (staging / "worktrees").is_dir():
        os.replace(staging / "worktrees", root / ".worktrees" / project)
    shutil.rmtree(staging, ignore_errors=True)

    blob_path.unlink()
    return repo


def _now() -> datetime:
    return datetime.now(timezone.utc)
