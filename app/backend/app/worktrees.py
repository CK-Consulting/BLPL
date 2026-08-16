"""One working copy per person, on a project several people share.

Two members of a project were editing the same directory. What kept that from
corrupting anything was the one-run-per-project rule — a lock, doing the job an
isolation boundary should be doing — so the cost of sharing was that a colleague's
stage 6 blocked yours, and the reward for removing the lock would have been two
runs writing the same ``.pipeline/`` files at once.

Git already had the answer. A project is a repository, so each member gets a
**worktree**: their own checkout, on their own branch, sharing one object store.
Not a copy — the history and objects are shared, so this costs a checkout rather
than a clone, and the branches are ordinary branches that merge the ordinary way.

    projects/baseboard/            the repository, and the owner's checkout
    projects/.worktrees/baseboard/u7/   another member's, on branch user/7

What this buys, in order of how much it matters:

* Two people can run stages at once without touching each other's artefacts.
* "What did they change" is a diff between branches, which is a question git was
  built to answer — and the reason the whole project is text.
* The encryption phase has something to seal: a workspace with a lifecycle, not
  a directory that is always there.

What it does not buy is merging. Two people editing the same design still have to
reconcile it, and this makes that a visible git operation rather than a silent
last-write-wins. That is the honest trade: the conflict was always there, and
sharing a directory hid it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .projects import ProjectError

# Kept out of the projects root, behind a dot, so every listing that walks the
# root and skips dotfiles keeps working unchanged — a worktree is not a project
# and must never appear as one.
WORKTREES_DIR = ".worktrees"


def branch_for(user_id: int) -> str:
    """The branch a user's worktree tracks.

    Namespaced under user/ so it cannot collide with a branch someone pushed —
    'main' and 'user/7' can coexist, 'main' and 'shaun' invite an accident.
    """
    return f"user/{user_id}"


def path_for(projects_root: Path, project_name: str, user_id: int, is_owner: bool) -> Path:
    """Where this user's checkout of this project lives.

    The owner keeps the repository directory itself. Giving everyone a worktree
    including the owner would be tidier and would move every existing project on
    disk, so the owner's checkout stays where it has always been — and every
    other member gets one beside it.
    """
    if is_owner:
        return Path(projects_root) / project_name
    return Path(projects_root) / WORKTREES_DIR / project_name / f"u{user_id}"


def ensure(projects_root: Path, project_name: str, user_id: int, is_owner: bool) -> Path:
    """This user's checkout, creating it on first use.

    Created lazily rather than when someone joins a project: a member who never
    opens it should not cost a checkout, and someone who was invited and has not
    accepted has no business having one.
    """
    root = Path(projects_root)
    repo = root / project_name
    target = path_for(root, project_name, user_id, is_owner)
    if is_owner or target.is_dir():
        # The owner's path is the repository directory itself, so nothing is
        # created and nothing about git is required. Demanding a repo here broke
        # every project that is a plain directory — which is a legitimate state,
        # and one the tests rely on.
        return target

    if not (repo / ".git").is_dir():
        raise ProjectError(
            f"project {project_name!r} is not a git working copy, so it cannot be "
            "shared into a separate checkout"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    branch = branch_for(user_id)
    existing = _run(repo, "branch", "--list", branch).strip()
    args = ["worktree", "add"]
    if existing:
        # Their branch survived a worktree being removed — reuse it rather than
        # starting again, or the work on it becomes unreachable.
        args += [str(target), branch]
    else:
        args += ["-b", branch, str(target), "HEAD"]
    _run(repo, *args)
    return target


def remove(projects_root: Path, project_name: str, user_id: int) -> bool:
    """Drop someone's checkout when they lose access.

    The *branch* is deliberately left behind. Removing a worktree is about
    access; deleting the branch would destroy work, and "you were removed from a
    project" should not also mean "your unmerged changes are gone".
    """
    root = Path(projects_root)
    repo = root / project_name
    target = path_for(root, project_name, user_id, is_owner=False)
    if not target.is_dir():
        return False
    _run(repo, "worktree", "remove", "--force", str(target))
    return True


def list_for(projects_root: Path, project_name: str) -> list[str]:
    """Worktree paths git knows about, for diagnosing a mismatch with the
    filesystem — git's registry and the directories can disagree if someone
    deletes one by hand."""
    repo = Path(projects_root) / project_name
    out = _run(repo, "worktree", "list", "--porcelain")
    return [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")]


def prune(projects_root: Path, project_name: str) -> None:
    """Forget worktrees whose directories are gone. Cheap, and it stops
    `worktree add` refusing over a path nobody can see any more."""
    _run(Path(projects_root) / project_name, "worktree", "prune")


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise ProjectError(f"git {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout
