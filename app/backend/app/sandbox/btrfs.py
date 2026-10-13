"""Phase 4, step 4: a btrfs subvolume per project, with a quota and snapshots.

docs/architecture-v2.md, "Storage". Each project's storage becomes its own
subvolume under a storage root, so it can carry a hard size limit (a qgroup)
and be snapshotted in O(1) before every run:

    <root>/<project>            the subvolume: repository and worktrees
    <root>/.snapshots/<project>/<label>   read-only snapshots

Creating subvolumes, setting limits and deleting snapshots need
CAP_SYS_ADMIN, which nothing else in the deployment has or should have. So
the btrfs work is done by ``StorageHelper``, a single-purpose service with
that one capability, no network and no secrets. It mounts only the storage
root, listens on a Unix socket, and takes a project name and a verb. It
derives every path from the name and never accepts a path from its caller.
The control plane reaches it through ``HelperStorage``, which implements
``ProjectStorage``.

Every btrfs command goes through one ``Runner``, so tests check the exact
commands against a fake, and so the code never shells out by any other
route. A real-filesystem test runs where ``BLPL_TEST_BTRFS_ROOT`` points at a
btrfs path with quotas enabled and the helper's privilege. Without one the
test skips and says so, rather than pretending.
"""

from __future__ import annotations

import json
import os
import re
import socketserver
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .driver import Mount, WorkspaceKey
from .storage import WORKSPACE_MOUNT

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
SNAPSHOTS = ".snapshots"
# How many pre-run snapshots a project keeps. Old ones are deleted when a new
# one is taken. A snapshot costs only what changed since, but each one pins its
# blocks.
KEEP_SNAPSHOTS = 5


class StorageError(RuntimeError):
    pass


Runner = Callable[[list[str]], str]


def run_btrfs(argv: list[str]) -> str:
    """The real runner: btrfs-progs, failing loudly."""
    if not argv or argv[0] != "btrfs":
        raise StorageError("only btrfs commands are run here")
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise StorageError(f"{' '.join(argv[:3])}: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout


@dataclass
class BtrfsStore:
    """The btrfs operations for one storage root. Runs inside the helper."""

    root: Path
    run: Runner = run_btrfs

    def _project(self, name: str) -> Path:
        if not _NAME.fullmatch(name or ""):
            raise StorageError(f"invalid project name {name!r}")
        return self.root / name

    def _snapdir(self, name: str) -> Path:
        self._project(name)
        return self.root / SNAPSHOTS / name

    def create(self, name: str) -> dict:
        """The project's subvolume, made if absent. A plain directory already
        at that path is refused: it would have to be migrated, and that is done
        while the project is sealed (unseal into a new subvolume), not here."""
        path = self._project(name)
        if path.exists():
            if not self._is_subvolume(path):
                raise StorageError(f"{name} exists and is not a subvolume; migrate it while sealed")
            return {"path": str(path), "created": False}
        self.run(["btrfs", "subvolume", "create", str(path)])
        return {"path": str(path), "created": True}

    def _is_subvolume(self, path: Path) -> bool:
        try:
            self.run(["btrfs", "subvolume", "show", str(path)])
            return True
        except StorageError:
            return False

    def set_limit(self, name: str, limit_bytes: int | None) -> dict:
        path = self._project(name)
        value = "none" if not limit_bytes else str(int(limit_bytes))
        self.run(["btrfs", "qgroup", "limit", value, str(path)])
        return {"limit": limit_bytes}

    def usage(self, name: str) -> dict:
        """Referenced and exclusive bytes, from the project's own qgroup."""
        path = self._project(name)
        sub_id = self._subvolume_id(path)
        out = self.run(["btrfs", "qgroup", "show", "-reF", "--raw", str(path)])
        for line in out.splitlines():
            cols = line.split()
            if cols and cols[0] == f"0/{sub_id}":
                limit = cols[3] if len(cols) > 3 else "none"
                return {"referenced": int(cols[1]), "exclusive": int(cols[2]),
                        "limit": None if limit in ("none", "-") else int(limit)}
        raise StorageError(f"no qgroup for {name} (are quotas enabled?)")

    def _subvolume_id(self, path: Path) -> int:
        out = self.run(["btrfs", "subvolume", "show", str(path)])
        m = re.search(r"Subvolume ID:\s*(\d+)", out)
        if not m:
            raise StorageError(f"could not read the subvolume id of {path}")
        return int(m.group(1))

    def snapshot(self, name: str, label: str) -> dict:
        """A read-only snapshot, then the oldest beyond KEEP_SNAPSHOTS deleted."""
        if not _LABEL.fullmatch(label or ""):
            raise StorageError(f"invalid snapshot label {label!r}")
        path, snapdir = self._project(name), self._snapdir(name)
        snapdir.mkdir(parents=True, exist_ok=True)
        dest = snapdir / label
        if dest.is_symlink():
            raise StorageError(f"snapshot {label} is a symlink; refusing it")
        if dest.exists():
            # The same run asking again: a retry of a reclaimed run whose first
            # worker took this snapshot and died. It is reused if it is what
            # this would have made, a read-only subvolume in this project's
            # snapshot directory. Anything else there is an error (Codex,
            # PR #20).
            if not self._is_subvolume(dest) or "ro=true" not in self.run(
                    ["btrfs", "property", "get", "-ts", str(dest), "ro"]):
                raise StorageError(f"{label} exists and is not this project's snapshot")
            return {"snapshot": label, "dropped": [], "existing": True}
        self.run(["btrfs", "subvolume", "snapshot", "-r", str(path), str(dest)])
        dropped = []
        for old in self.snapshots(name)["snapshots"][:-KEEP_SNAPSHOTS]:
            self.run(["btrfs", "subvolume", "delete", str(snapdir / old)])
            dropped.append(old)
        return {"snapshot": label, "dropped": dropped}

    def snapshots(self, name: str) -> dict:
        snapdir = self._snapdir(name)
        if not snapdir.is_dir():
            return {"snapshots": []}
        entries = sorted((p for p in snapdir.iterdir() if p.is_dir()),
                         key=lambda p: p.stat().st_mtime)
        return {"snapshots": [p.name for p in entries]}

    def restore(self, name: str, label: str) -> dict:
        """Put a snapshot back as the project's storage.

        A writable snapshot of the snapshot is made beside the project first,
        then the two are swapped by rename, then the old one is deleted. A
        failure before the swap leaves the project as it was. The workspace
        must be stopped while this runs: the control plane stops it first."""
        if not _LABEL.fullmatch(label or ""):
            raise StorageError(f"invalid snapshot label {label!r}")
        path, snap = self._project(name), self._snapdir(name) / label
        if not snap.is_dir():
            raise StorageError(f"no snapshot {label} of {name}")
        incoming = self.root / f".{name}.restoring"
        outgoing = self.root / f".{name}.replaced"
        for leftover in (incoming, outgoing):
            if leftover.exists():
                self.run(["btrfs", "subvolume", "delete", str(leftover)])
        self.run(["btrfs", "subvolume", "snapshot", str(snap), str(incoming)])
        os.rename(path, outgoing)
        os.rename(incoming, path)
        self.run(["btrfs", "subvolume", "delete", str(outgoing)])
        return {"restored": label}


# -- the helper: a socket, a project name, a verb ------------------------------------


_VERBS = {"create", "set_limit", "usage", "snapshot", "snapshots", "restore"}


class StorageHelper:
    def __init__(self, store: BtrfsStore):
        self.store = store

    def handle(self, request: dict) -> dict:
        verb = request.get("verb")
        if verb not in _VERBS:
            return {"ok": False, "error": f"unknown verb {verb!r}"}
        try:
            project = str(request.get("project", ""))
            if verb == "set_limit":
                out = self.store.set_limit(project, request.get("limit"))
            elif verb in ("snapshot", "restore"):
                out = getattr(self.store, verb)(project, str(request.get("label", "")))
            else:
                out = getattr(self.store, verb)(project)
            return {"ok": True, **out}
        except (StorageError, OSError) as exc:
            return {"ok": False, "error": str(exc)}


def serve_helper(socket_path: str, helper: StorageHelper) -> socketserver.UnixStreamServer:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            line = self.rfile.readline(64 * 1024)
            try:
                request = json.loads(line or b"{}")
                if not isinstance(request, dict):
                    raise ValueError
            except ValueError:
                reply = {"ok": False, "error": "request must be one JSON object per line"}
            else:
                reply = helper.handle(request)
            self.wfile.write(json.dumps(reply).encode() + b"\n")

    p = Path(socket_path)
    if p.exists() or p.is_symlink():
        p.unlink()
    server = socketserver.ThreadingUnixStreamServer(str(p), Handler)
    os.chmod(p, 0o660)
    return server


# -- the control plane's side --------------------------------------------------------


class HelperClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    def call(self, verb: str, project: str, **kw) -> dict:
        import socket

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(600)
            s.connect(self.socket_path)
            s.sendall(json.dumps({"verb": verb, "project": project, **kw}).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        reply = json.loads(data or b"{}")
        if not reply.get("ok"):
            raise StorageError(reply.get("error") or f"{verb} failed")
        return reply


class Storage(Protocol):
    def snapshot(self, project: str, label: str) -> dict: ...


@dataclass
class HelperStorage:
    """ProjectStorage backed by the helper: one subvolume per project.

    ``host_root`` is the storage root as the daemon's host sees it, for the
    workspace's bind mount; the helper sees the same directory at its own
    mount point and is only ever told a project name."""

    client: HelperClient
    host_root: str
    enforces_quota: bool = True

    async def ensure(self, key: WorkspaceKey, *, quota_bytes: int | None = None) -> Mount:
        import asyncio

        await asyncio.to_thread(self.client.call, "create", key.project)
        if quota_bytes is not None:
            await asyncio.to_thread(self.client.call, "set_limit", key.project, limit=quota_bytes)
        return Mount(kind="bind", source=f"{self.host_root}/{key.project}",
                     target=WORKSPACE_MOUNT)

    async def usage(self, key: WorkspaceKey) -> int | None:
        import asyncio

        r = await asyncio.to_thread(self.client.call, "usage", key.project)
        return int(r["referenced"])

    def snapshot(self, project: str, label: str) -> dict:
        return self.client.call("snapshot", project, label=label)

    def restore(self, project: str, label: str) -> dict:
        return self.client.call("restore", project, label=label)

    def snapshots(self, project: str) -> list[str]:
        return self.client.call("snapshots", project)["snapshots"]


def main() -> None:  # pragma: no cover - the helper container's entry point
    import logging

    logging.basicConfig(level=logging.INFO)
    root = Path(os.environ.get("BLPL_STORAGE_ROOT", "/storage"))
    sock = os.environ.get("BLPL_STORAGE_SOCKET", "/run/blpl-storage/storage.sock")
    server = serve_helper(sock, StorageHelper(BtrfsStore(root)))
    logging.getLogger("blpl.storage").info("serving %s on %s", root, sock)
    server.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
