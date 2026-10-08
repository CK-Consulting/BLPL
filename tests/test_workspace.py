"""Project files, encrypted when nobody is in them.

The constraint that shaped the design is the one asserted hardest here:
**diffing must not break**. KiCad was chosen because its files are text, which is
what makes the pipeline parametric and deterministic at low cost, and a
git-crypt-style per-file filter would have turned every schematic diff into
binary noise. So the encryption boundary is storage, not files — git only ever
sees plaintext, and `test_diffing_survives_a_seal_and_open` is the test that says
so.

What is being protected is narrow and worth stating: a disk, backup or snapshot
of a project nobody had open. A project someone *is* in is plaintext on disk with
its key in the API process, and no test here should be read as claiming
otherwise.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app import workspace  # noqa: E402


def _seal_now(client, project="mine"):
    """Seal through the API, which is the only thing holding the key."""
    return client.post(f"/api/projects/{project}/seal").json()


def _share(owner, invitee, project="mine"):
    owner.post(f"/api/projects/{project}/members", json={"email": "other@example.com"})
    inv = invitee.get("/api/invitations").json()[0]["id"]
    invitee.post(f"/api/invitations/{inv}/accept")


# ---------------------------------------------------------------- the boundary


def test_a_sealed_project_leaves_no_plaintext_on_disk(unlocked):
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.put("/api/projects/mine/files/secret.md", json={"content": "MPU6050\n"})

    assert _seal_now(unlocked)["sealed"] is True

    assert not (main.PROJECTS_ROOT / "mine").exists()
    blob = workspace.sealed_path(main.PROJECTS_ROOT, "mine")
    assert blob.exists()
    assert b"MPU6050" not in blob.read_bytes()


def test_a_sealed_project_is_423_not_404(unlocked):
    """Locked, not missing and not forbidden. 404 would say it is gone and 403
    would say it is not yours — both are wrong, and both send the client
    somewhere other than the button that opens it."""
    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.put("/api/projects/mine/files/a.md", json={"content": "x\n"})
    _seal_now(unlocked)

    r = unlocked.get("/api/projects/mine/files")
    assert r.status_code == 423
    assert "sealed" in r.json()["detail"]
    # Named in a header, because these routes also answer 423 for "your session
    # has no key" and the two want opposite responses from the client.
    assert r.headers["X-BLPL-Sealed"] == "mine"

    # And it is still listed: a project you cannot currently read is not a
    # project that has disappeared.
    assert "mine" in [p["id"] for p in unlocked.get("/api/projects").json()]


def test_opening_restores_the_files(unlocked):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.put("/api/projects/mine/files/a.md", json={"content": "hello\n"})
    _seal_now(unlocked)

    assert unlocked.post("/api/projects/mine/open").json() == {"ok": True, "already_open": False}

    assert unlocked.get("/api/projects/mine/files/a.md").json()["content"] == "hello\n"


def test_opening_an_open_project_is_a_no_op(unlocked):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    body = unlocked.post("/api/projects/mine/open").json()
    assert body == {"ok": True, "already_open": True}


# ------------------------------------------------------- the whole point of it


def test_diffing_survives_a_seal_and_open(unlocked, second_user):
    """The constraint the whole design is shaped around.

    Sealing archives the repository and every worktree together — forced, not
    chosen, because a worktree holds a `.git` *file* pointing into the
    repository's object store. Seal them apart and the link breaks; seal the
    project alone and each member's checkout is orphaned. This asserts the link
    survives, which is the difference between encryption at rest and losing the
    reason KiCad was chosen.
    """
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.put("/api/projects/mine/files/board.md", json={"content": "# board\n"})
    unlocked.post("/api/projects/mine/git/commit", json={"message": "owner's work"})
    _share(unlocked, second_user)
    second_user.put("/api/projects/mine/files/board.md", json={"content": "# board\ntheirs\n"})

    _seal_now(unlocked)
    assert not (main.PROJECTS_ROOT / ".worktrees" / "mine").exists()
    unlocked.post("/api/projects/mine/open")

    repo = main.PROJECTS_ROOT / "mine"
    assert subprocess.run(["git", "log", "--oneline"], cwd=repo, capture_output=True,
                          text=True).returncode == 0
    assert subprocess.run(["git", "status"], cwd=repo, capture_output=True,
                          text=True).returncode == 0

    # The member's checkout still points into the object store it shares.
    theirs = main.PROJECTS_ROOT / ".worktrees" / "mine" / "u2"
    assert theirs.is_dir()
    assert subprocess.run(["git", "status"], cwd=theirs, capture_output=True,
                          text=True).returncode == 0
    assert second_user.get("/api/projects/mine/files/board.md").json()["content"] == "# board\ntheirs\n"


def test_the_key_is_the_project_key_not_a_shared_one(unlocked):
    """Sealed under the per-project key, so a blob is only openable by that
    project's members — not by anyone the server would hand a key to."""
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.put("/api/projects/mine/files/a.md", json={"content": "x\n"})
    _seal_now(unlocked)

    with __import__("pytest").raises(workspace.WorkspaceError, match="not the key"):
        workspace.unseal(main.PROJECTS_ROOT, "mine", os.urandom(32))


# ------------------------------------------------------------- when it re-seals


def test_locking_seals_what_you_had_open(unlocked):
    """"I am done" is the clearest signal there is."""
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.put("/api/projects/mine/files/a.md", json={"content": "x\n"})

    body = unlocked.post("/api/auth/lock").json()

    assert body["sealed"] == ["mine"]
    assert not (main.PROJECTS_ROOT / "mine").exists()


def test_locking_does_not_seal_a_project_a_colleague_is_in(unlocked, second_user):
    """One project is one directory tree shared by its members. Sealing on your
    lock alone would delete the files out from under whoever is still editing."""
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    _share(unlocked, second_user)
    second_user.get("/api/projects/mine/files")  # they are in it

    assert unlocked.post("/api/auth/lock").json()["sealed"] == []
    assert (main.PROJECTS_ROOT / "mine").is_dir()

    # Once the last of them leaves, it goes.
    assert second_user.post("/api/auth/lock").json()["sealed"] == ["mine"]
    assert not (main.PROJECTS_ROOT / "mine").exists()


def test_idle_workspaces_are_sealed_without_being_asked(unlocked):
    """The common case is not locking — it is closing a laptop and walking off,
    which would otherwise leave a project decrypted until someone came back."""
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.put("/api/projects/mine/files/a.md", json={"content": "x\n"})

    entry = main.workspaces.all_open()[0]
    entry.last_touched -= timedelta(hours=2)

    with main.SessionFactory() as session:
        for idle in main.workspaces.idle():
            main._seal_workspace(session, idle.workspace)
        session.commit()

    assert not (main.PROJECTS_ROOT / "mine").exists()


def test_the_idle_clock_restarts_when_you_touch_a_project(unlocked):
    """Otherwise a long editing session gets sealed mid-edit at the half hour."""
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    main.workspaces.all_open()[0].last_touched -= timedelta(hours=2)

    unlocked.get("/api/projects/mine/files")

    assert main.workspaces.idle() == []


def test_a_run_in_flight_stops_a_seal(unlocked):
    """The worker is another container reading those files. Sealing mid-run
    deletes the working copy out from under a stage."""
    import app.main as main
    from conftest import enqueue_only

    unlocked.post("/api/projects/init", json={"name": "mine"})
    enqueue_only(unlocked, "/api/projects/mine/stages/doctor")

    assert _seal_now(unlocked)["sealed"] is False
    assert (main.PROJECTS_ROOT / "mine").is_dir()


def test_a_project_created_and_left_alone_still_gets_sealed(unlocked):
    """Only opening a sealed project used to register it, so a project created
    and never re-opened would have sat in plaintext for its whole life."""
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "fresh"})

    assert main.workspaces.is_open("fresh")
    assert main.workspaces.key_for("fresh") is not None


# ------------------------------------------------------------------- neighbours


def test_a_member_without_a_grant_is_told_so(unlocked, second_user):
    """A member with permission but no wrapped copy of the key cannot decrypt.
    Possible for projects predating the key machinery, and worth naming rather
    than surfacing as a decryption failure."""
    import app.main as main
    from app import grants
    from app.models import Project

    unlocked.post("/api/projects/init", json={"name": "mine"})
    _share(unlocked, second_user)
    second_user.get("/api/projects/mine/files")
    _seal_now(unlocked)

    with main.SessionFactory() as session:
        project = session.query(Project).filter_by(name="mine").one()
        grants.revoke_grant(session, project, 2)
        session.commit()

    r = second_user.post("/api/projects/mine/open")
    assert r.status_code == 409
    assert "no key for this project" in r.json()["detail"]


def test_a_non_member_cannot_open_or_seal(unlocked, second_user):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    _seal_now(unlocked)

    assert second_user.post("/api/projects/mine/open").status_code == 404
    assert second_user.post("/api/projects/mine/seal").status_code == 404


def test_an_interrupted_seal_never_loses_the_project(unlocked):
    """The one failure that would be unforgivable: a truncated blob beside a
    directory that has already been deleted. The blob is written to a temporary
    file and moved into place, so a crash leaves the old blob or the new one."""
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.put("/api/projects/mine/files/a.md", json={"content": "irreplaceable\n"})
    key = main.workspaces.key_for("mine")

    real_replace = os.replace
    def die_before_swapping_it_in(src, dst, *a, **kw):
        if str(dst).endswith(".blob"):
            raise OSError("disk full")
        return real_replace(src, dst, *a, **kw)

    os.replace = die_before_swapping_it_in
    try:
        # Raised as a WorkspaceError since #46, so callers keep the workspace
        # registered; the guarantee is the files below, not the type.
        with __import__("pytest").raises((OSError, workspace.WorkspaceError)):
            workspace.seal(main.PROJECTS_ROOT, "mine", key)
    finally:
        os.replace = real_replace

    assert (main.PROJECTS_ROOT / "mine" / "a.md").read_text() == "irreplaceable\n"


def test_unsealing_over_an_open_project_does_not_overwrite_it(unlocked):
    """Extract-then-unlink means a crash between the two leaves both the blob
    and the directory. The next open must keep the newer directory rather than
    replacing it with the older copy in the blob."""
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.put("/api/projects/mine/files/a.md", json={"content": "old\n"})
    key = main.workspaces.key_for("mine")
    workspace.seal(main.PROJECTS_ROOT, "mine", key)
    workspace.unseal(main.PROJECTS_ROOT, "mine", key)

    (main.PROJECTS_ROOT / "mine" / "a.md").write_text("newer\n")
    # A blob left behind by a crash mid-restore.
    workspace.seal(main.PROJECTS_ROOT, "mine", key)
    workspace.unseal(main.PROJECTS_ROOT, "mine", key)
    (main.PROJECTS_ROOT / "mine" / "a.md").write_text("newest\n")
    blob = workspace.sealed_path(main.PROJECTS_ROOT, "mine")
    blob.parent.mkdir(parents=True, exist_ok=True)

    assert (main.PROJECTS_ROOT / "mine" / "a.md").read_text() == "newest\n"


def test_a_locked_session_is_not_mistaken_for_a_sealed_project(unlocked):
    """Both answer 423 on these routes and they mean different things: one is
    "decrypt this", the other is "go and unlock". A client that confuses them
    tries to open a project it has no key for, on every request."""
    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.post("/api/auth/lock")

    r = unlocked.post("/api/projects/mine/stages/doctor")
    assert r.status_code == 423
    assert "X-BLPL-Sealed" not in r.headers


def _git_repo_with_history(path: Path) -> None:
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    for i in range(40):
        (path / f"doc{i}.md").write_text(f"# doc {i}\n" * 200)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=path, check=True, env=env)


def test_concurrent_opens_restore_one_whole_repository(tmp_path):
    # Six opens of one sealed project, all at once — what the frontend sent on
    # the day this broke. Each used to unseal into the same staging directory,
    # rmtree another's half-finished extract, and one of them moved the
    # remainder into place: a repository with its design files and no HEAD.
    from concurrent.futures import ThreadPoolExecutor

    root = tmp_path / "projects"
    _git_repo_with_history(root / "p")
    key = os.urandom(32)
    workspace.seal(root, "p", key)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: workspace.unseal(root, "p", key), range(6)))

    assert all(r == root / "p" for r in results)
    assert not workspace.is_sealed(root, "p")
    head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root / "p",
                          capture_output=True, text=True)
    assert head.returncode == 0, head.stderr
    assert head.stdout.strip() == "main"
    assert len(list((root / "p").glob("doc*.md"))) == 40
    staging = root / workspace.SEALED_DIR / ".restore" / "p"
    assert not staging.exists() or not any(staging.iterdir())


def test_a_failed_restore_leaves_no_plaintext_behind(tmp_path, monkeypatch):
    # Codex P1 on #45: staging was removed only on success, and each restore
    # used a fresh name that later restores ignored — so an extract that died
    # part way left decrypted files beside the blob for good.
    import tarfile

    root = tmp_path / "projects"
    _git_repo_with_history(root / "p")
    key = os.urandom(32)
    workspace.seal(root, "p", key)

    real = tarfile.TarFile.extractall

    def die_part_way(self, path, *a, **kw):
        real(self, path, *a, **kw)
        raise OSError("disk full")

    monkeypatch.setattr(tarfile.TarFile, "extractall", die_part_way)
    try:
        workspace.unseal(root, "p", key)
    except (OSError, workspace.WorkspaceError):
        pass
    monkeypatch.undo()

    # The blob and its lock file, and no decrypted file anywhere beside them.
    left = {f.name for f in (root / workspace.SEALED_DIR).rglob("*") if f.is_file()}
    assert left == {"p.blob", ".p.lock"}, left
    assert not (root / "p").exists()
    assert workspace.unseal(root, "p", key) == root / "p"


def _plant_leftovers(root: Path, project: str) -> list[Path]:
    # What a killed process leaves: a restore that never reached its finally,
    # in today's layout and in the fixed name restores used before it.
    current = root / workspace.SEALED_DIR / ".restore" / project / "tmpdead" / "repo"
    legacy = root / workspace.SEALED_DIR / f".{project}.restore" / "repo"
    # #45's layout: mkdtemp(prefix=".{project}.restore-") — eight random chars.
    randomized = root / workspace.SEALED_DIR / f".{project}.restore-k3x_9q0z" / "repo"
    for d in (current, legacy, randomized):
        d.mkdir(parents=True)
        (d / "secret.md").write_text("plaintext")
    return [current, legacy, randomized]


def test_leftover_plaintext_is_reaped_by_the_next_unseal(tmp_path):
    root = tmp_path / "projects"
    _git_repo_with_history(root / "p")
    key = os.urandom(32)
    workspace.seal(root, "p", key)
    planted = _plant_leftovers(root, "p")

    workspace.unseal(root, "p", key)
    assert not any(p.exists() for p in planted)


def test_leftover_plaintext_is_reaped_by_the_next_seal(tmp_path):
    root = tmp_path / "projects"
    _git_repo_with_history(root / "p")
    planted = _plant_leftovers(root, "p")

    workspace.seal(root, "p", os.urandom(32))
    assert not any(p.exists() for p in planted)


def test_reaping_one_project_never_touches_another(tmp_path):
    # Names may contain dots, so a prefix match on ".p.restore" would also
    # have matched a project called "p.restore-x".
    root = tmp_path / "projects"
    _git_repo_with_history(root / "p")
    others = _plant_leftovers(root, "p.restore-x")
    # The nearest miss for #45's pattern: a project whose own name supplies
    # eight characters after ".p.restore-".
    others += _plant_leftovers(root, "p.restore-abcdefgh")

    workspace.seal(root, "p", os.urandom(32))
    assert all(o.exists() for o in others)


def _project_with_worktree(root: Path) -> Path:
    _git_repo_with_history(root / "p")
    wt = root / ".worktrees" / "p" / "alice"
    wt.mkdir(parents=True)
    (wt / "alice.md").write_text("alice's uncommitted work")
    return wt / "alice.md"


def _kill_at_second_promotion(monkeypatch, root: Path) -> None:
    # Fail whichever of the two promoting renames comes second, so the test
    # lands in the window between them under any ordering — not only the one
    # the code happens to use today.
    real = workspace.os.replace
    targets = {root / "p", root / ".worktrees" / "p"}
    seen = []

    def replace(src, dst):
        if Path(dst) in targets:
            seen.append(dst)
            if len(seen) == 2:
                raise OSError("killed")
        return real(src, dst)

    monkeypatch.setattr(workspace.os, "replace", replace)


def test_a_restore_killed_before_the_repository_lands_keeps_the_worktrees(tmp_path, monkeypatch):
    # Codex P1 on #46: the repository used to land first. Killed before the
    # worktrees followed, the next open saw a repository, returned it, and the
    # next seal archived the project without anyone's worktree.
    root = tmp_path / "projects"
    note = _project_with_worktree(root)
    key = os.urandom(32)
    workspace.seal(root, "p", key)

    _kill_at_second_promotion(monkeypatch, root)
    try:
        workspace.unseal(root, "p", key)
    except (OSError, workspace.WorkspaceError):
        pass
    monkeypatch.undo()

    workspace.unseal(root, "p", key)
    assert note.read_text() == "alice's uncommitted work"
    workspace.seal(root, "p", key)
    workspace.unseal(root, "p", key)
    assert note.read_text() == "alice's uncommitted work"


def test_a_seal_killed_part_way_never_leaves_half_a_repository(tmp_path, monkeypatch):
    # The original corruption, from the other side: a seal that dies while
    # deleting the repository leaves a partial one beside a valid blob, and the
    # next open would use it. The repository is now renamed aside in one step.
    root = tmp_path / "projects"
    note = _project_with_worktree(root)
    key = os.urandom(32)
    note.write_text("latest")

    # Killed at the second step of removing the plaintext, whatever it is: a
    # rename aside (today), or an rmtree that has deleted part of the tree.
    real_replace, real_rmtree = workspace.os.replace, workspace.shutil.rmtree
    steps = []

    def replace(src, dst):
        if Path(src) in {root / "p", root / ".worktrees" / "p"}:
            steps.append(src)
            if len(steps) == 2:
                raise OSError("killed")
        return real_replace(src, dst)

    def rmtree(path, *a, **kw):
        if Path(path) == root / "p":
            (root / "p" / ".git" / "HEAD").unlink()
            raise OSError("killed")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(workspace.os, "replace", replace)
    monkeypatch.setattr(workspace.shutil, "rmtree", rmtree)
    try:
        workspace.seal(root, "p", key)
    except (OSError, workspace.WorkspaceError):
        pass
    monkeypatch.undo()
    assert workspace.is_sealed(root, "p")
    if (root / "p").exists():
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root / "p", capture_output=True)
        assert head.returncode == 0, "a partial repository was left looking open"

    workspace.unseal(root, "p", key)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root / "p", capture_output=True)
    assert head.returncode == 0
    assert note.read_text() == "latest"
    leftovers = [f for f in (root / workspace.SEALED_DIR).rglob("*") if f.is_file()]
    assert {f.name for f in leftovers} <= {".p.lock"}, leftovers


def test_a_partial_seal_that_is_retried_keeps_the_repository(tmp_path, monkeypatch):
    # Codex P1 on #46: the repository renamed aside, the worktree rename
    # failed, and the idle sweep — still holding the key — retried. The retry
    # reaped the renamed repository as staging and archived the worktrees
    # alone over the complete blob.
    root = tmp_path / "projects"
    note = _project_with_worktree(root)
    key = os.urandom(32)
    real = workspace.os.replace

    def replace(src, dst):
        if Path(src) == root / ".worktrees" / "p":
            raise OSError("worktree rename failed")
        return real(src, dst)

    monkeypatch.setattr(workspace.os, "replace", replace)
    try:
        workspace.seal(root, "p", key)
    except (OSError, workspace.WorkspaceError):
        pass
    monkeypatch.undo()

    workspace.seal(root, "p", key)  # the sweep's retry
    workspace.unseal(root, "p", key)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root / "p", capture_output=True)
    assert head.returncode == 0, "the retry lost the repository"
    assert note.read_text() == "alice's uncommitted work"


def test_a_read_only_folder_is_still_sealed_away(tmp_path):
    # Codex P1 on #46: rmtree(ignore_errors=True) gave up on a directory the
    # backend user could not write into, and seal reported success over the
    # plaintext it left.
    root = tmp_path / "projects"
    _git_repo_with_history(root / "p")
    ro = root / "p" / "vendor"
    ro.mkdir()
    (ro / "notes.md").write_text("plaintext")
    ro.chmod(0o555)
    key = os.urandom(32)

    workspace.seal(root, "p", key)
    left = [f for f in root.rglob("*") if f.is_file() and f.suffix not in {".blob", ".lock"}]
    assert left == [], left

    workspace.unseal(root, "p", key)
    assert (ro / "notes.md").read_text() == "plaintext"
    ro.chmod(0o755)


def test_plaintext_that_cannot_be_removed_fails_the_seal(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    _git_repo_with_history(root / "p")
    monkeypatch.setattr(workspace.shutil, "rmtree", lambda *a, **kw: None)
    try:
        workspace.seal(root, "p", os.urandom(32))
    except workspace.WorkspaceError:
        return
    raise AssertionError("seal reported success while decrypted files remained")
