"""Git-backed projects: the sync spine for roaming between workstations.

Uses real git against local file:// remotes — no network, no mocks — because the
whole value of this module is that it drives git correctly, and a mock would just
assert that we call the functions we call. The traversal guard and the
clean/dirty/ahead/behind accounting are what carry risk, so those are the focus.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app.projects import Projects, ProjectError  # noqa: E402


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_remote(tmp_path: Path) -> Path:
    """A bare repo with one commit on main, standing in for a real git remote."""
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "design.md").write_text("# design\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "initial")

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True)
    return bare


@pytest.fixture
def projects(tmp_path: Path) -> Projects:
    p = Projects(tmp_path / "projects")
    return p


def test_project_dir_refuses_traversal(projects: Projects) -> None:
    with pytest.raises(ProjectError):
        projects.project_dir("../escape")


def test_clone_brings_down_the_remote(tmp_path: Path, projects: Projects) -> None:
    remote = _make_remote(tmp_path)
    dest = projects.clone("dev04", f"file://{remote}", "main")
    assert (dest / "design.md").is_file()
    assert projects.exists("dev04")


def test_clone_refuses_to_clobber_an_existing_project(tmp_path: Path, projects: Projects) -> None:
    remote = _make_remote(tmp_path)
    projects.clone("dev04", f"file://{remote}", "main")
    with pytest.raises(ProjectError, match="already exists"):
        projects.clone("dev04", f"file://{remote}", "main")


def test_a_fresh_clone_is_clean_and_synced(tmp_path: Path, projects: Projects) -> None:
    remote = _make_remote(tmp_path)
    projects.clone("dev04", f"file://{remote}", "main")
    st = projects.status("dev04")
    assert st.branch == "main"
    assert not st.dirty
    assert st.ahead == 0 and st.behind == 0
    assert st.has_remote


def test_an_edit_shows_as_dirty_then_clean_after_commit(tmp_path: Path, projects: Projects) -> None:
    remote = _make_remote(tmp_path)
    d = projects.clone("dev04", f"file://{remote}", "main")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")

    (d / "design.md").write_text("# design\nmore\n")
    assert projects.status("dev04").dirty is True

    out = projects.commit_all("dev04", "edit design")
    assert out is not None
    assert projects.status("dev04").dirty is False


def test_commit_on_a_clean_tree_is_a_noop(tmp_path: Path, projects: Projects) -> None:
    remote = _make_remote(tmp_path)
    d = projects.clone("dev04", f"file://{remote}", "main")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    assert projects.commit_all("dev04", "nothing changed") is None


def test_a_local_commit_shows_as_ahead(tmp_path: Path, projects: Projects) -> None:
    remote = _make_remote(tmp_path)
    d = projects.clone("dev04", f"file://{remote}", "main")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "design.md").write_text("# design\nlocal change\n")
    projects.commit_all("dev04", "local edit")
    st = projects.status("dev04")
    assert st.ahead == 1 and st.behind == 0


def test_commit_then_push_clears_ahead(tmp_path: Path, projects: Projects) -> None:
    remote = _make_remote(tmp_path)
    d = projects.clone("dev04", f"file://{remote}", "main")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "design.md").write_text("# design\npushed change\n")
    projects.commit_all("dev04", "to push")
    projects.push("dev04")
    assert projects.status("dev04").ahead == 0


def test_init_local_creates_a_git_repo_with_no_remote(projects: Projects) -> None:
    d = projects.init_local("scratch")
    assert (d / ".git").is_dir()
    st = projects.status("scratch")
    assert st.has_remote is False
    assert st.ahead == 0 and st.behind == 0


def test_pull_brings_down_a_remote_commit(tmp_path: Path, projects: Projects) -> None:
    remote = _make_remote(tmp_path)
    projects.clone("dev04", f"file://{remote}", "main")

    # A second clone commits and pushes, so the remote moves ahead of our copy.
    other = tmp_path / "other"
    subprocess.run(["git", "clone", f"file://{remote}", str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "o@o")
    _git(other, "config", "user.name", "o")
    (other / "new.md").write_text("added elsewhere\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "from another workstation")
    _git(other, "push")

    projects.pull("dev04")
    assert (projects.project_dir("dev04") / "new.md").is_file()


def test_operations_on_a_non_git_dir_are_rejected(projects: Projects) -> None:
    projects.project_dir("plain")  # resolves, but never gets a .git
    (projects.root / "plain").mkdir()
    with pytest.raises(ProjectError, match="not a git project"):
        projects.status("plain")
