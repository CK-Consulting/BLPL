"""The Lifecycle pane edits the table Stage 8 hands the audit: docs/lifecycle.md.

The pane and the run used to agree by both defaulting to <project>/lifecycle.md.
With the table moved out of Stage 0's reach into docs/, they agree by both
asking project_manifest.lifecycle_table — tested here so that a pane reading
the old place cannot quietly show an empty table beside a full one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")


def test_the_pane_reads_the_docs_table(client, tmp_path: Path) -> None:
    import app.main as main

    assert main._lifecycle_path(tmp_path) == tmp_path / "docs" / "lifecycle.md"


def test_a_project_from_before_the_move_keeps_its_root_table(client, tmp_path: Path) -> None:
    import app.main as main

    (tmp_path / "lifecycle.md").write_text("| MPN |\n|---|\n", encoding="utf-8")
    assert main._lifecycle_path(tmp_path) == tmp_path / "lifecycle.md"
