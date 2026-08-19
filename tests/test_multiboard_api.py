"""Board resolution over the HTTP API.

Split from test_multiboard.py because these need the webapp extras, and a
module-level importorskip takes the whole file with it — which would have
silently skipped fifty tests that need nothing but the standard library.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")  # webapp extras must be installed for these tests.

MANIFEST = """
## Boards

- base — the carrier
- sensor (optional)
- radio (optional)

## Mates

- base.J3 <-> sensor.J1

## Configurations

- full: base, sensor, radio
"""



def test_resolve_board_refuses_the_same_things_the_cli_does(tmp_path):
    """The app and the command line must agree, or a board that runs in one
    fails in the other for reasons nobody can see."""
    from fastapi import HTTPException

    from app.main import _resolve_board

    (tmp_path / "project.md").write_text(MANIFEST)
    with pytest.raises(HTTPException) as ambiguous:
        _resolve_board(tmp_path, None)
    assert "more than one board" in ambiguous.value.detail

    with pytest.raises(HTTPException) as unknown:
        _resolve_board(tmp_path, "ghost")
    assert unknown.value.status_code == 404

    assert _resolve_board(tmp_path, "sensor") == "sensor"


def test_a_single_board_project_needs_no_board_over_the_api(tmp_path):
    from app.main import _resolve_board

    (tmp_path / "design.md").write_text("# a board\n")
    assert _resolve_board(tmp_path, None) is None


def test_emitted_boards_are_told_apart_by_stem(tmp_path):
    """Without the filter the viewer shows whichever file is newest — a
    different board than the one on screen, with nothing saying so."""
    from app.main import _latest_with_origin

    pipeline = tmp_path / ".pipeline"
    pipeline.mkdir()
    (pipeline / "shield_base_2026-08-19_000000Z.kicad_pcb").write_text("base")
    (pipeline / "shield_sensor_2026-08-19_010000Z.kicad_pcb").write_text("sensor")

    newest, _ = _latest_with_origin(pipeline, ".kicad_pcb")
    assert newest.read_text() == "sensor"          # newest wins with no filter

    base, _ = _latest_with_origin(pipeline, ".kicad_pcb", stem_contains="_base_")
    assert base.read_text() == "base"              # …and the filter picks correctly


def test_project_level_artifacts_survive_a_board_filter():
    """The cross-board report is about this board as much as any other, so
    filtering to a board must not hide it."""
    from app.main import _is_project_level

    assert _is_project_level("crossboard.json")
    assert not _is_project_level("bom.base.json")
