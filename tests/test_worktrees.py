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


def _git(cwd, *args):
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def test_a_members_save_is_committed_on_their_branch_not_the_owners(unlocked, second_user):
    """The edit lands in the member's worktree, so that is where it is committed.

    It used to be committed in the owner's checkout instead: the member's file
    stayed uncommitted, and whatever the owner had uncommitted was swept into a
    commit named after somebody else's edit.
    """
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    _share(unlocked, second_user)
    owner_dir = main.PROJECTS_ROOT / "mine"
    (owner_dir / "owner-draft.md").write_text("not ready\n")

    r = second_user.put("/api/projects/mine/files/theirs.md", json={"content": "work\n"})
    assert r.json()["committed"] is True

    theirs = main.PROJECTS_ROOT / worktrees.WORKTREES_DIR / "mine" / "u2"
    assert _git(theirs, "status", "--porcelain").strip() == ""
    assert "edit: theirs.md" in _git(theirs, "log", "-1", "--format=%s")
    # The owner's draft is still the owner's, uncommitted, and on no one's branch.
    assert "owner-draft.md" in _git(owner_dir, "status", "--porcelain")
    assert "theirs.md" not in _git(owner_dir, "ls-files")


def test_the_commit_button_commits_the_callers_own_checkout(unlocked, second_user):
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    _share(unlocked, second_user)
    second_user.get("/api/projects/mine/files")
    theirs = main.PROJECTS_ROOT / worktrees.WORKTREES_DIR / "mine" / "u2"
    (theirs / "loose.md").write_text("x\n")
    (main.PROJECTS_ROOT / "mine" / "owner-draft.md").write_text("not ready\n")

    r = second_user.post("/api/projects/mine/git/commit", json={"message": "mine"})
    assert r.json()["committed"] is True
    assert "loose.md" in _git(theirs, "show", "--name-only", "--format=", "HEAD")
    assert "owner-draft.md" in _git(main.PROJECTS_ROOT / "mine", "status", "--porcelain")


def test_commit_and_push_publishes_the_members_commit_not_the_owners_branch(unlocked, second_user, tmp_path):
    """"Commit + Push" is two requests. Both have to act on the member's checkout,
    or the push reports success having published nothing of theirs."""
    import subprocess

    import app.main as main

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    unlocked.post("/api/projects/init", json={"name": "mine"})
    owner_dir = main.PROJECTS_ROOT / "mine"
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=owner_dir, check=True)
    assert unlocked.post("/api/projects/mine/git/push").status_code == 200
    owner_branch = _git(owner_dir, "branch", "--show-current").strip()
    owner_head = _git(remote, "rev-parse", owner_branch).strip()

    _share(unlocked, second_user)
    second_user.get("/api/projects/mine/files")
    theirs = main.PROJECTS_ROOT / worktrees.WORKTREES_DIR / "mine" / "u2"
    (theirs / "theirs.md").write_text("work\n")
    assert second_user.post("/api/projects/mine/git/commit", json={"message": "m"}).json()["committed"]
    r = second_user.post("/api/projects/mine/git/push")
    assert r.status_code == 200, r.text

    member_branch = worktrees.branch_for(2)
    assert "theirs.md" in _git(remote, "ls-tree", "-r", "--name-only", member_branch)
    assert _git(remote, "rev-parse", owner_branch).strip() == owner_head

    status = second_user.get("/api/projects/mine/git/status").json()
    assert status["branch"] == member_branch
    assert status["ahead"] == 0 and status["has_remote"]


def test_pulling_a_branch_that_was_never_pushed_says_so(unlocked, second_user, tmp_path):
    import subprocess

    import app.main as main

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    unlocked.post("/api/projects/init", json={"name": "mine"})
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=main.PROJECTS_ROOT / "mine", check=True)
    _share(unlocked, second_user)
    r = second_user.post("/api/projects/mine/git/pull")
    assert r.status_code == 400 and "push it first" in r.json()["detail"]
