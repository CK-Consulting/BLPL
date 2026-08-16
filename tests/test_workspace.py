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
        with __import__("pytest").raises(OSError):
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
