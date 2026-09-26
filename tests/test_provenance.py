"""Stage 6 records which commit of each library a board was emitted against."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from blpl.core import provenance

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _repo(path: Path, lib_dir: str = ".") -> str:
    """A one-commit repository at ``path`` holding a symbol library under ``lib_dir``."""
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    lib = path / lib_dir / "Lib.kicad_symdir"
    lib.mkdir(parents=True)
    (lib / "U1.kicad_sym").write_text("(kicad_symbol_lib)")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return run("rev-parse", "HEAD").stdout.strip()


def test_a_clean_root_records_its_commit(tmp_path):
    commit = _repo(tmp_path / "modules")
    entry = provenance.describe_root(tmp_path / "modules")
    assert entry["git"]["commit"] == commit
    assert entry["git"]["dirty"] is False
    assert entry["git"]["changed_files"] == 0


def test_an_uncommitted_footprint_makes_the_root_dirty(tmp_path):
    """The case this exists for: drawn in the desktop, never committed."""
    _repo(tmp_path / "modules")
    (tmp_path / "modules" / "New.pretty").mkdir()
    (tmp_path / "modules" / "New.pretty" / "X.kicad_mod").write_text("(footprint)")
    entry = provenance.describe_root(tmp_path / "modules")
    assert entry["git"]["dirty"] is True
    assert entry["git"]["changed_files"] == 1


def test_dirt_outside_the_root_does_not_count(tmp_path):
    """A design markdown edit does not make the project's libraries/ uncommitted."""
    project = tmp_path / "project"
    _repo(project, "libraries")
    (project / "core.md").write_text("edited\n")

    entry = provenance.describe_root(project / "libraries")
    assert entry["git"]["repo"] == str(project.resolve())
    assert entry["git"]["dirty"] is False


def test_a_root_outside_git_says_so_rather_than_vanishing(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    entry = provenance.describe_root(plain)
    assert entry["git"] is None
    assert entry["reason"]


def test_a_missing_root_says_so(tmp_path):
    entry = provenance.describe_root(tmp_path / "nope")
    assert entry["git"] is None
    assert entry["reason"] == "root does not exist"


def test_credentials_in_a_remote_url_are_not_recorded(tmp_path):
    _repo(tmp_path / "modules")
    subprocess.run(
        ["git", "-C", str(tmp_path / "modules"), "remote", "add", "origin",
         "https://user:ghp_secret@github.com/o/blpl-modules.git"],
        check=True,
    )
    entry = provenance.describe_root(tmp_path / "modules")
    assert entry["git"]["remote"] == "https://github.com/o/blpl-modules.git"
    assert "ghp_secret" not in json.dumps(entry)


def test_a_root_used_for_both_kinds_is_listed_once_in_search_order(tmp_path):
    _repo(tmp_path / "a")
    _repo(tmp_path / "b")
    out = provenance.library_provenance([tmp_path / "a", tmp_path / "b"], [tmp_path / "b"])
    assert [Path(e["root"]).name for e in out] == ["a", "b"]
    assert out[0]["used_for"] == ["symbols"]
    assert out[1]["used_for"] == ["symbols", "footprints"]
