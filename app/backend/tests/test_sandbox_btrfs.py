"""Phase 4, step 4: per-project btrfs storage behind a single-purpose helper.

The btrfs commands are checked against a fake runner that keeps a model of
subvolumes, so the sequence and the paths are exact without needing root. A
real-filesystem test runs only where BLPL_TEST_BTRFS_ROOT names a btrfs path
with quotas enabled that this process may manage. Elsewhere it skips and
says why.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from app.sandbox import btrfs, jobs
from app.sandbox.btrfs import BtrfsStore, HelperClient, HelperStorage, StorageError, StorageHelper
from app.sandbox.driver import WorkspaceKey
from app.sandbox.fake import FakeDriver


class FakeBtrfs:
    """Subvolumes as directories, plus a record of every command."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.subvolumes: set[str] = set()
        self.limits: dict[str, str] = {}

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        assert argv[0] == "btrfs"
        sub, verb = argv[1], argv[2]
        if (sub, verb) == ("subvolume", "create"):
            Path(argv[3]).mkdir()
            self.subvolumes.add(argv[3])
        elif (sub, verb) == ("subvolume", "show"):
            if argv[3] not in self.subvolumes:
                raise StorageError("not a subvolume")
            return f"Name: x\n\tSubvolume ID:\t\t{256 + sorted(self.subvolumes).index(argv[3])}\n"
        elif (sub, verb) == ("subvolume", "snapshot"):
            src, dest = argv[-2], argv[-1]
            shutil.copytree(src, dest, symlinks=True)
            self.subvolumes.add(dest)
        elif (sub, verb) == ("subvolume", "delete"):
            shutil.rmtree(argv[3])
            self.subvolumes.discard(argv[3])
        elif (sub, verb) == ("qgroup", "limit"):
            self.limits[argv[4]] = argv[3]
        elif (sub, verb) == ("qgroup", "show"):
            path = argv[-1]
            sid = 256 + sorted(self.subvolumes).index(path)
            return ("qgroupid rfer excl max_rfer max_excl\n"
                    f"0/{sid} 16384 8192 {self.limits.get(path, 'none')} none\n")
        return ""


@pytest.fixture
def store(tmp_path):
    fake = FakeBtrfs()
    return BtrfsStore(tmp_path / "storage", run=fake), fake


def test_a_project_gets_a_subvolume_a_limit_and_its_usage(store):
    s, fake = store
    s.root.mkdir()
    assert s.create("board")["created"] is True
    assert s.create("board")["created"] is False  # idempotent
    s.set_limit("board", 10 * 1024**3)
    assert fake.limits[str(s.root / "board")] == str(10 * 1024**3)
    assert s.usage("board") == {"referenced": 16384, "exclusive": 8192, "limit": 10 * 1024**3}
    s.set_limit("board", None)
    assert fake.limits[str(s.root / "board")] == "none"


def test_a_plain_directory_is_not_taken_over(store):
    s, _ = store
    (s.root / "board").mkdir(parents=True)
    with pytest.raises(StorageError, match="migrate it while sealed"):
        s.create("board")


@pytest.mark.parametrize("name", ["../x", ".snapshots", "", "a/b", "/etc"])
def test_names_never_become_paths(store, name):
    s, fake = store
    s.root.mkdir()
    with pytest.raises(StorageError):
        s.create(name)
    assert fake.calls == []


def test_snapshots_are_read_only_and_bounded(store):
    s, fake = store
    s.root.mkdir()
    s.create("board")
    for i in range(btrfs.KEEP_SNAPSHOTS + 2):
        out = s.snapshot("board", f"pre-run{i}")
        os.utime(s.root / ".snapshots" / "board" / f"pre-run{i}", (i, i))
    assert out["dropped"]
    kept = s.snapshots("board")["snapshots"]
    assert len(kept) == btrfs.KEEP_SNAPSHOTS and "pre-run0" not in kept
    snaps = [c for c in fake.calls if c[1:3] == ["subvolume", "snapshot"]]
    assert all(c[3] == "-r" for c in snaps)
    with pytest.raises(StorageError):
        s.snapshot("board", "../escape")


def test_restore_puts_the_snapshot_back_and_drops_the_replaced_one(store):
    s, _ = store
    s.root.mkdir()
    s.create("board")
    (s.root / "board" / "design.md").write_text("good\n")
    s.snapshot("board", "pre-r1")
    (s.root / "board" / "design.md").write_text("wrecked by a run\n")
    s.restore("board", "pre-r1")
    assert (s.root / "board" / "design.md").read_text() == "good\n"
    assert not (s.root / ".board.replaced").exists()
    assert not (s.root / ".board.restoring").exists()
    with pytest.raises(StorageError):
        s.restore("board", "pre-nothing")


def test_the_helper_takes_a_name_and_a_verb_over_its_socket(store):
    s, _ = store
    s.root.mkdir()
    sock = Path(tempfile.mkdtemp(prefix="blpl-st-")) / "s.sock"
    server = btrfs.serve_helper(str(sock), StorageHelper(s))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        storage = HelperStorage(HelperClient(str(sock)), host_root="/srv/blpl/storage")
        m = asyncio.run(storage.ensure(WorkspaceKey("board"), quota_bytes=1024**3))
        assert (m.source, m.target) == ("/srv/blpl/storage/board", "/workspace")
        assert asyncio.run(storage.usage(WorkspaceKey("board"))) == 16384
        storage.snapshot("board", "pre-x")
        assert storage.snapshots("board") == ["pre-x"]
        with pytest.raises(StorageError, match="invalid project name"):
            HelperClient(str(sock)).call("create", "../../etc")
        with pytest.raises(StorageError, match="unknown verb"):
            HelperClient(str(sock)).call("rm", "board")
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(sock.parent, ignore_errors=True)


def test_a_run_is_preceded_by_a_snapshot_and_does_not_start_without_one(tmp_path):
    cfg = jobs.WorkspaceRunConfig(image="i", projects_root=tmp_path / "p",
                                  host_projects_root="/srv/p")
    order: list[str] = []
    d = FakeDriver(script=lambda call: (order.append("run"), (0, []))[1])

    async def snap(label):
        order.append(f"snapshot {label}")

    async def poll():
        return "running"

    lines: list[str] = []
    assert asyncio.run(jobs.run_job_in_container(
        d, cfg, project_name="board", run_id="run_7", cmd=["x"], env={},
        log=lines.append, poll=poll, snapshot=snap)) == 0
    assert order == ["snapshot pre-run_7", "run"]

    async def broken(label):
        raise StorageError("quota exceeded")

    d2 = FakeDriver()
    lines.clear()
    assert asyncio.run(jobs.run_job_in_container(
        d2, cfg, project_name="board", run_id="run_8", cmd=["x"], env={},
        log=lines.append, poll=poll, snapshot=broken)) == -1
    assert d2.calls == [] and "did not start" in "".join(lines)


def _btrfs_root_or_reason() -> str | None:
    root = os.environ.get("BLPL_TEST_BTRFS_ROOT")
    if not root:
        return ("no BLPL_TEST_BTRFS_ROOT: a real test needs a btrfs path with quotas "
                "enabled that this process may manage (CAP_SYS_ADMIN). The suite does not "
                "use sudo to make one.")
    try:
        btrfs.run_btrfs(["btrfs", "qgroup", "show", root])
    except StorageError as exc:
        return f"{root}: quotas are not usable here ({exc})"
    return None


@pytest.mark.skipif(_btrfs_root_or_reason() is not None, reason=_btrfs_root_or_reason() or "")
def test_on_a_real_btrfs_filesystem(tmp_path):  # pragma: no cover - needs the setup above
    root = Path(os.environ["BLPL_TEST_BTRFS_ROOT"]) / f"blpl-test-{os.getpid()}"
    root.mkdir()
    s = BtrfsStore(root)
    try:
        s.create("board")
        s.set_limit("board", 64 * 1024**2)
        (root / "board" / "f").write_bytes(b"x" * 1024)
        s.snapshot("board", "pre-1")
        (root / "board" / "f").write_bytes(b"y")
        s.restore("board", "pre-1")
        assert (root / "board" / "f").read_bytes() == b"x" * 1024
        assert s.usage("board")["referenced"] > 0
    finally:
        for p in sorted(root.rglob("*"), reverse=True):
            try:
                btrfs.run_btrfs(["btrfs", "subvolume", "delete", str(p)])
            except StorageError:
                pass
        shutil.rmtree(root, ignore_errors=True)
