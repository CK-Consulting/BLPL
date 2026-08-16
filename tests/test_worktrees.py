"""Two people on one project, working without colliding.

Before this, members shared a directory and the one-run-per-project rule was what
stopped them corrupting each other's `.pipeline/` — a lock standing in for an
isolation boundary that did not exist. The cost was that a colleague's stage 6
blocked yours; the cost of removing the lock would have been two runs writing the
same files.

Git already had the answer, and it is the reason the whole project is text: each
member gets a worktree on their own branch, sharing one object store. What that
does *not* solve is merging — two people editing the same design still have to
reconcile it. This makes that a visible git operation rather than a silent
last-write-wins, which is an improvement rather than a fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

from conftest import sign_in

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app import worktrees  # noqa: E402


def _share(owner, invitee, project="mine"):
    owner.post(f"/api/projects/{project}/members", json={"email": "other@example.com"})
    inv = invitee.get("/api/invitations").json()[0]["id"]
    invitee.post(f"/api/invitations/{inv}/accept")


def test_the_owner_keeps_the_repository_directory(unlocked, tmp_path):
    """Giving everyone a worktree including the owner would be tidier and would
    move every project already on disk. The owner's checkout stays put."""
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.put("/api/projects/mine/files/a.md", json={"content": "owner\n"})

    assert (main.PROJECTS_ROOT / "mine" / "a.md").read_text() == "owner\n"


def test_a_member_gets_their_own_checkout(unlocked, second_user):
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    _share(unlocked, second_user)

    second_user.get("/api/projects/mine/files")  # first touch creates it

    theirs = main.PROJECTS_ROOT / worktrees.WORKTREES_DIR / "mine" / "u2"
    assert theirs.is_dir()
    assert (theirs / ".git").exists()  # a worktree links back rather than cloning


def test_edits_do_not_land_in_each_others_files(unlocked, second_user):
    """The property the lock was standing in for."""
    unlocked.post("/api/projects/init", json={"name": "mine"})
    _share(unlocked, second_user)

    unlocked.put("/api/projects/mine/files/notes.md", json={"content": "mine\n"})
    second_user.put("/api/projects/mine/files/notes.md", json={"content": "theirs\n"})

    assert unlocked.get("/api/projects/mine/files/notes.md").json()["content"] == "mine\n"
    assert second_user.get("/api/projects/mine/files/notes.md").json()["content"] == "theirs\n"


def test_two_members_can_both_have_a_run_in_flight(unlocked, second_user):
    """What the per-project lock used to forbid. Their artefacts are in different
    directories now, so there is nothing to serialise."""
    from conftest import enqueue_only

    unlocked.post("/api/projects/init", json={"name": "mine"})
    _share(unlocked, second_user)

    enqueue_only(unlocked, "/api/projects/mine/stages/doctor")
    enqueue_only(second_user, "/api/projects/mine/stages/doctor")

    mine = [r for r in unlocked.get("/api/projects/mine/runs").json() if r["running"]]
    assert len(mine) == 2  # both are visible in the project's history...
    assert {r["by"] for r in mine} == {"test@example.com", "other@example.com"}


def test_one_person_still_cannot_race_themselves(unlocked):
    """Still per user: two of your own stages on your own checkout would write the
    same .pipeline/ files."""
    from conftest import enqueue_only

    unlocked.post("/api/projects/init", json={"name": "mine"})
    enqueue_only(unlocked, "/api/projects/mine/stages/doctor")

    second = unlocked.post("/api/projects/mine/stages/stage2")
    assert second.status_code == 409
    assert "already have a run in flight" in second.json()["detail"]


def test_losing_access_removes_the_checkout_but_keeps_the_branch(unlocked, second_user):
    """Being removed from a project should not also destroy unmerged work."""
    import subprocess

    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    _share(unlocked, second_user)
    second_user.put("/api/projects/mine/files/theirs.md", json={"content": "work\n"})

    other_id = [
        m["id"] for m in unlocked.get("/api/projects/mine/members").json()["members"] if not m["is_you"]
    ][0]
    unlocked.delete(f"/api/projects/mine/members/{other_id}")

    assert not (main.PROJECTS_ROOT / worktrees.WORKTREES_DIR / "mine" / f"u{other_id}").is_dir()
    branches = subprocess.run(
        ["git", "branch", "--list", worktrees.branch_for(other_id)],
        cwd=main.PROJECTS_ROOT / "mine",
        capture_output=True,
        text=True,
    ).stdout
    assert worktrees.branch_for(other_id) in branches


def test_a_worktree_is_not_listed_as_a_project(unlocked, second_user):
    """They live under a dotted directory precisely so every listing that walks
    the projects root and skips dotfiles keeps working."""
    unlocked.post("/api/projects/init", json={"name": "mine"})
    _share(unlocked, second_user)
    second_user.get("/api/projects/mine/files")

    assert [p["id"] for p in unlocked.get("/api/projects").json()] == ["mine"]
    assert worktrees.WORKTREES_DIR not in unlocked.get("/api/projects").text


def test_a_new_project_has_a_commit_to_branch_from(unlocked):
    """`git init` alone leaves an unborn HEAD, which reads as a repository until
    something asks it for a commit — and sharing a brand-new project then failed
    with a git error nobody would connect to "it has never been committed"."""
    import subprocess

    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "fresh"})
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=main.PROJECTS_ROOT / "fresh",
        capture_output=True,
        text=True,
    )
    assert head.returncode == 0 and head.stdout.strip()
