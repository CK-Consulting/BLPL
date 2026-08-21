"""Version control: what is recorded, what the badge means, and reading it back.

Written after a real project was inspected and found to have four `chat:`
commits, a 53 MB .git, and a permanently-lit "uncommitted" badge — the last of
which is what made it look as though nothing was being recorded at all.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app.agent import ToolContext, ToolExecutor, default_tools
from app.projects import Projects
from app.references import FilesystemSandbox, ReferenceManifest


def _git(d: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=d, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    projects = Projects(tmp_path / "root")
    d = projects.init_local("dev04")
    (d / "overview.md").write_text("# Board\n\nPINMAP: PA0 = SCL\n", encoding="utf-8")
    projects.commit_all("dev04", "first pinmap")
    (d / "overview.md").write_text("# Board\n\nPINMAP: PA0 = SDA\n", encoding="utf-8")
    projects.commit_all("dev04", "second pinmap")
    return d


def _ctx(project: Path) -> ToolContext:
    return ToolContext(
        project_id="dev04",
        project_dir=project,
        sandbox=FilesystemSandbox(
            manifest=ReferenceManifest.empty(project_id="dev04", workspace_root=project)
        ),
    )


def _call(ctx: ToolContext, _tool: str, **args):
    async def approve(_r):
        return True

    ex = ToolExecutor(default_tools(), ctx, approve=approve)
    from blpl.core.llm_chat import ToolUseBlock

    return asyncio.run(ex(ToolUseBlock(id="t1", name=_tool, input=args)))


def test_a_value_overwritten_long_ago_is_still_recoverable(repo) -> None:
    """The whole point. A pinmap agreed and then replaced falls out of the
    conversation window — summarisation drops tables first — but it is in the
    history, and now something can go and get it."""
    ctx = _ctx(repo)
    hist = json.loads(_call(ctx, "file_history", path="overview.md").content)
    assert [c["what"] for c in hist["commits"]] == ["second pinmap", "first pinmap"]

    old = _call(ctx, "read_file_version", path="overview.md", commit=hist["commits"][1]["commit"])
    assert not old.is_error and "PA0 = SCL" in old.content


def test_a_diff_says_what_moved(repo) -> None:
    hist = json.loads(_call(_ctx(repo), "file_history", path="overview.md").content)
    res = _call(_ctx(repo), "diff_file", path="overview.md", since=hist["commits"][1]["commit"])
    assert "-PINMAP: PA0 = SCL" in res.content and "+PINMAP: PA0 = SDA" in res.content


def test_history_refuses_a_ref_that_is_really_a_git_option(repo) -> None:
    """Refs arrive from a model. `--output=...` is a ref-shaped string that is
    not a ref, and git would happily treat it as one."""
    res = _call(_ctx(repo), "read_file_version", path="overview.md", commit="--output=/tmp/x")
    assert res.is_error and "commit reference" in res.content


def test_history_will_not_reach_outside_the_project(repo) -> None:
    res = _call(_ctx(repo), "file_history", path="../elsewhere.md")
    assert res.is_error


def test_a_file_with_no_history_says_so_rather_than_failing(repo) -> None:
    (repo / "new.md").write_text("fresh", encoding="utf-8")
    out = json.loads(_call(_ctx(repo), "file_history", path="new.md").content)
    assert out["commits"] == [] and "no commits" in out["note"]


# -- the badge ---------------------------------------------------------------


def test_a_conversation_does_not_count_as_unsaved_design_work(tmp_path: Path) -> None:
    """This is why the badge read as permanently on: the conversation log is
    appended to on every turn, so the tree is never clean."""
    projects = Projects(tmp_path / "root")
    d = projects.init_local("dev04")
    (d / ".blpl" / "conversations").mkdir(parents=True)
    (d / ".blpl" / "conversations" / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (d / ".blpl" / "llm_usage.jsonl").write_text("{}\n", encoding="utf-8")
    (d / ".pipeline").mkdir()
    (d / ".pipeline" / "bom.json").write_text("{}", encoding="utf-8")
    assert projects.status("dev04").dirty is False


def test_an_edited_design_document_does_count(tmp_path: Path) -> None:
    projects = Projects(tmp_path / "root")
    d = projects.init_local("dev04")
    (d / "overview.md").write_text("# Board\n", encoding="utf-8")
    assert projects.status("dev04").dirty is True


def test_bookkeeping_is_still_committed_even_though_it_is_not_flagged(tmp_path: Path) -> None:
    """Not flagged is not the same as not kept: the conversation holds the full
    text of every proposal, which is worth having in the history."""
    projects = Projects(tmp_path / "root")
    d = projects.init_local("dev04")
    (d / ".blpl" / "conversations").mkdir(parents=True)
    (d / ".blpl" / "conversations" / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (d / "overview.md").write_text("# Board\n", encoding="utf-8")
    projects.commit_all("dev04", "both")
    tracked = _git(d, "ls-files").split()
    assert ".blpl/conversations/a.jsonl" in tracked


# -- the baseline ignore rules -----------------------------------------------


def test_a_new_project_ignores_its_own_output(tmp_path: Path) -> None:
    projects = Projects(tmp_path / "root")
    d = projects.init_local("dev04")
    (d / ".pipeline").mkdir()
    (d / ".pipeline" / "bom.json").write_text("{}", encoding="utf-8")
    (d / "overview.md").write_text("# Board\n", encoding="utf-8")
    projects.commit_all("dev04", "work")
    tracked = _git(d, "ls-files").split()
    assert "overview.md" in tracked
    assert not any(t.startswith(".pipeline/") for t in tracked)


def test_quarantined_downloads_stay_out_of_history_but_the_ledger_does_not(
    tmp_path: Path,
) -> None:
    """An uncleared download is the one thing that should never reach permanent
    history. What it was and where it came from should."""
    projects = Projects(tmp_path / "root")
    d = projects.init_local("dev04")
    (d / "retrieved").mkdir()
    (d / "retrieved" / "abc-part.pdf").write_bytes(b"%PDF-1.4")
    (d / "retrieved" / "quarantine.json").write_text("[]", encoding="utf-8")
    projects.commit_all("dev04", "fetch")
    tracked = _git(d, "ls-files").split()
    assert "retrieved/quarantine.json" in tracked
    assert "retrieved/abc-part.pdf" not in tracked


def test_an_existing_project_is_cleaned_up_and_says_what_it_untracked(
    tmp_path: Path,
) -> None:
    """Adding the file changes nothing on its own — git keeps tracking what it
    already tracks — so the untracking is the part that does the work."""
    projects = Projects(tmp_path / "root")
    d = projects.init_local("dev04")
    (d / ".gitignore").unlink()
    (d / ".pipeline").mkdir()
    (d / ".pipeline" / "bom.json").write_text("{}", encoding="utf-8")
    projects.commit_all("dev04", "before")
    assert ".pipeline/bom.json" in _git(d, "ls-files").split()

    untracked = projects.apply_baseline_gitignore("dev04")
    assert untracked == [".pipeline/bom.json"]
    assert ".pipeline/bom.json" not in _git(d, "ls-files").split()
    # Untracked, never deleted: the next run expects to find its own artifacts.
    assert (d / ".pipeline" / "bom.json").is_file()
    # And still in the history it was already part of. This stops the repository
    # growing; it does not rewrite what is there.
    assert ".pipeline/bom.json" in _git(d, "show", "--name-only", "--format=", "HEAD~1")


def test_applying_the_baseline_twice_changes_nothing_the_second_time(tmp_path: Path) -> None:
    projects = Projects(tmp_path / "root")
    projects.init_local("dev04")
    d = projects.project_dir("dev04")
    before = _git(d, "rev-parse", "HEAD")
    assert projects.apply_baseline_gitignore("dev04") == []
    assert _git(d, "rev-parse", "HEAD") == before


def test_a_project_with_its_own_rules_keeps_them(tmp_path: Path) -> None:
    projects = Projects(tmp_path / "root")
    d = projects.init_local("dev04")
    (d / ".gitignore").write_text("# mine\nscratch/\n", encoding="utf-8")
    projects.commit_all("dev04", "my rules")
    projects.apply_baseline_gitignore("dev04")
    rules = (d / ".gitignore").read_text(encoding="utf-8")
    assert "scratch/" in rules and ".pipeline/" in rules
