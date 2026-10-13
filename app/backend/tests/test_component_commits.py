"""Two live issues in the component routes, closed (docs/architecture-v2.md).

1. Attach, update and detach committed the whole index. Anything staged at the
   time, such as the entire tree the KiCad bridge's snapshot stages, went into
   a commit named "Reference component …".
2. Detach removed ``<checkout>/.git/modules/…``. In a member's worktree that
   path does not exist (``.git`` is a file), so nothing was removed, the error
   was swallowed, and the store leaked, so a re-attach there failed. (The plan
   also feared the owner's store was shared with members. git 2.55 gives each
   worktree its own, so it is not; the tests pin that too.)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import components, worktrees
from app.gitidentity import GitIdentity
from app.projects import Projects

IDENT = GitIdentity("Test", "test@example.com")


def _git(d: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=d, capture_output=True, text=True,
                          check=True).stdout


@pytest.fixture
def setup(tmp_path):
    data = tmp_path / "data"
    root = data / "projects"
    project = Projects(root).init_local("dev04", identity=IDENT)
    components.add_document(data, 7, "PART1", "ds.pdf", b"%PDF here", identity=IDENT)
    return data, root, project


def _committed(d: Path) -> set[str]:
    return set(_git(d, "show", "--name-only", "--format=", "HEAD").split())


def test_attach_commits_only_what_it_changed(setup):
    data, _, project = setup
    (project / "notes.md").write_text("unrelated, staged, not for this commit\n")
    _git(project, "add", "notes.md")
    components.attach(project, data, 7, "PART1", identity=IDENT)
    assert _committed(project) == {".gitmodules", "datasheets/PART1"}
    # Still staged, still the person's to commit.
    assert "notes.md" in _git(project, "diff", "--cached", "--name-only")


def test_detach_commits_only_what_it_changed(setup):
    data, _, project = setup
    components.attach(project, data, 7, "PART1", identity=IDENT)
    (project / "notes.md").write_text("staged meanwhile\n")
    _git(project, "add", "notes.md")
    components.detach(project, "PART1", identity=IDENT)
    assert _committed(project) <= {".gitmodules", "datasheets/PART1"}
    assert "notes.md" not in _committed(project)
    assert components.attached(project) == {}


def _git_dir(d: Path) -> Path:
    return Path(_git(d, "rev-parse", "--absolute-git-dir").strip())


def _store(d: Path) -> Path:
    return _git_dir(d) / "modules" / "datasheets" / "PART1"


def test_detach_in_a_members_worktree_removes_that_worktrees_store(setup):
    data, root, project = setup
    components.attach(project, data, 7, "PART1", identity=IDENT)
    member = worktrees.ensure(root, "dev04", 9, is_owner=False)
    components.sync(member)
    store = _store(member)
    assert store.exists() and "worktrees" in store.parts  # its own, not the owner's
    components.detach(member, "PART1", identity=IDENT)
    assert not store.exists()
    assert _store(project).exists()  # the owner's checkout is untouched
    # And re-attaching there works, which a leaked store prevented.
    assert components.attach(member, data, 7, "PART1", identity=IDENT)["added"]


def test_detach_in_the_owners_checkout_leaves_a_members_submodule_working(setup):
    data, root, project = setup
    components.attach(project, data, 7, "PART1", identity=IDENT)
    member = worktrees.ensure(root, "dev04", 9, is_owner=False)
    components.sync(member)
    components.detach(project, "PART1", identity=IDENT)
    assert not _store(project).exists()
    assert (member / "datasheets" / "PART1" / "ds.pdf").read_bytes() == b"%PDF here"
    assert _git(member / "datasheets" / "PART1", "rev-parse", "HEAD").strip()


def test_detach_removes_the_store_so_reattach_works(setup):
    data, _, project = setup
    components.attach(project, data, 7, "PART1", identity=IDENT)
    components.detach(project, "PART1", identity=IDENT)
    assert not _store(project).exists()
    assert components.attach(project, data, 7, "PART1", identity=IDENT)["added"]
