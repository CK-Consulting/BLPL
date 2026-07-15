"""Git-backed projects with a server working copy.

The roaming requirement, restated as a data problem: your design markdown and the
board it generates must live somewhere that follows you between workstations, not
on whichever laptop you happen to be at. So they live on the server, in a working
copy under one projects root, and that working copy is backed by a git remote.
You edit markdown locally and push; the server pulls. Or you edit on the server
(via the app) and it commits and pushes. Either way, git is the sync spine and
the server is the always-on copy.

This module is a thin, auditable wrapper over the ``git`` CLI — not a
reimplementation of git, and not GitPython. Every method shells out to git with a
fixed argv (never a shell string), scoped to one project directory that is proven
to sit under the projects root before anything runs. Remotes and branches come
from blpl.toml, not from the request, so a caller cannot point the server at an
arbitrary URL.

Authentication to the remote is deliberately not handled here. On the deploy, git
uses whatever the environment gives it — an SSH deploy key mounted into the
container, or a credential helper. Keeping auth out of this file means no token
ever passes through application code or lands in a log line.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class ProjectError(RuntimeError):
    """A git or filesystem operation on a project failed. Carries the git stderr
    so the UI can show what actually went wrong instead of a generic 500."""


@dataclass(frozen=True)
class GitStatus:
    branch: str
    ahead: int          # local commits not yet pushed
    behind: int         # remote commits not yet pulled
    dirty: bool         # uncommitted working-tree changes
    has_remote: bool


class Projects:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def project_dir(self, name: str) -> Path:
        """Resolve a project name to a directory under the root, refusing escape.

        The name reaches here from a URL or config, so ``../`` is on the table.
        Resolve, then prove the result is still inside the root — the same guard
        the file endpoints use, kept in one place.
        """
        candidate = (self.root / name).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ProjectError(f"invalid project name {name!r}")
        return candidate

    def exists(self, name: str) -> bool:
        return self.project_dir(name).is_dir()

    # -- lifecycle -----------------------------------------------------------

    def clone(self, name: str, remote: str, branch: str = "main") -> Path:
        """Clone a remote into a new working copy. Fails if the dir already exists,
        rather than clobbering whatever is there."""
        dest = self.project_dir(name)
        if dest.exists():
            raise ProjectError(f"project {name!r} already exists at {dest}")
        self._git(self.root, "clone", "--branch", branch, remote, dest.name)
        return dest

    def init_local(self, name: str) -> Path:
        """Create a fresh git repo for a project that has no remote yet.

        A new design starts as a local working copy; a remote can be attached
        later by adding it to blpl.toml and pushing. `git init` here means even a
        local-only project has a history from its first commit.
        """
        dest = self.project_dir(name)
        if dest.exists():
            raise ProjectError(f"project {name!r} already exists at {dest}")
        dest.mkdir(parents=True)
        self._git(dest, "init", "-b", "main")
        return dest

    # -- sync ----------------------------------------------------------------

    def pull(self, name: str) -> str:
        """Fast-forward the working copy from its remote. Returns git's output.

        Deliberately not a merge or rebase of divergent history: if the server
        copy has diverged, that is a conflict a human resolves, not something the
        app papers over. --ff-only turns divergence into a clear error.
        """
        d = self._require(name)
        return self._git(d, "pull", "--ff-only")

    def commit_all(self, name: str, message: str) -> str | None:
        """Stage everything and commit. Returns the commit output, or None if the
        tree was clean (nothing to commit is not an error)."""
        d = self._require(name)
        self._git(d, "add", "-A")
        if not self._is_dirty(d):
            return None
        return self._git(d, "commit", "-m", message)

    def push(self, name: str) -> str:
        d = self._require(name)
        return self._git(d, "push")

    # -- inspection ----------------------------------------------------------

    def status(self, name: str) -> GitStatus:
        d = self._require(name)
        # --show-current works before the first commit (an "unborn" branch), where
        # rev-parse HEAD would fail. A just-initialized local project has a branch
        # name but no commit yet, and that is a valid state to report on.
        branch = self._git(d, "branch", "--show-current").strip()
        dirty = self._is_dirty(d)
        has_remote = bool(self._git(d, "remote").strip())
        ahead = behind = 0
        if has_remote:
            # Counts against the upstream, when one is configured. No upstream
            # (a fresh local repo) leaves both at zero, which is the truth.
            counts = self._git_allow_fail(
                d, "rev-list", "--left-right", "--count", "@{upstream}...HEAD"
            )
            if counts:
                parts = counts.split()
                if len(parts) == 2:
                    behind, ahead = int(parts[0]), int(parts[1])
        return GitStatus(branch=branch, ahead=ahead, behind=behind, dirty=dirty, has_remote=has_remote)

    # -- internals -----------------------------------------------------------

    def _require(self, name: str) -> Path:
        d = self.project_dir(name)
        if not (d / ".git").is_dir():
            raise ProjectError(f"{name!r} is not a git project")
        return d

    def _is_dirty(self, project_dir: Path) -> bool:
        return bool(self._git(project_dir, "status", "--porcelain").strip())

    def _git(self, cwd: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if proc.returncode != 0:
            raise ProjectError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout

    def _git_allow_fail(self, cwd: Path, *args: str) -> str:
        """For queries where a non-zero exit is a legitimate 'no answer' — e.g.
        counting against an upstream that isn't configured yet."""
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False, timeout=60
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
