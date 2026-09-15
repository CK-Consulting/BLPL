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


MULTI = """
## Boards

- base
- sensor (optional)

## Configurations

- full: base, sensor
"""


def _multi_board_project(unlocked, name="shield"):
    unlocked.post("/api/projects/init", json={"name": name})
    unlocked.put(f"/api/projects/{name}/files/project.md", json={"content": MULTI})
    unlocked.put(f"/api/projects/{name}/files/base/design.md", json={"content": "# base\n"})
    unlocked.put(f"/api/projects/{name}/files/sensor/design.md", json={"content": "# sensor\n"})
    return name


def test_running_the_whole_pipeline_needs_a_board_too(unlocked) -> None:
    """Each stage resolved a board but the range did not, so "run the pipeline"
    was the one control on a multi-board project that could not work — while
    every stage inside it individually could."""
    p = _multi_board_project(unlocked)
    # stage5→8 needs no LLM, so a 400 here is about the board and nothing else.
    r = unlocked.post(f"/api/projects/{p}/pipeline?from_stage=stage5&to_stage=stage8")
    assert r.status_code == 400
    assert "board" in str(r.json().get("detail", "")).lower()


def test_all_boards_is_a_pipeline_scope_only_a_multi_board_project_has(unlocked) -> None:
    """`board=all` runs every board and then the cross-board check, as the CLI
    does. A single-board project has no 'all' and says so rather than running
    its one board under a misleading label."""
    unlocked.post("/api/projects", json={"id": "single"})
    unlocked.put("/api/projects/single/files/design.md", json={"content": "# one\n"})
    r = unlocked.post("/api/projects/single/pipeline?from_stage=stage5&to_stage=stage8&board=all")
    assert r.status_code == 400
    assert "single-board" in str(r.json().get("detail", ""))


def test_the_review_panel_needs_a_board(unlocked) -> None:
    """The panel reads fixed artifact names out of .pipeline. Unqualified, it
    finds nothing on a multi-board project and reports there is nothing to
    review — true of the names it looked for, misleading about the board."""
    p = _multi_board_project(unlocked)
    r = unlocked.post(f"/api/projects/{p}/review-panel")
    assert r.status_code == 400
    assert "board" in str(r.json().get("detail", "")).lower()


def test_the_review_panel_reads_and_writes_the_board_it_was_asked_about(tmp_path):
    """Artifact naming is shared with the rest of the pipeline; the panel had
    its own hardcoded names."""
    from blpl.agent import review_panel

    pipeline = tmp_path / ".pipeline"
    pipeline.mkdir()
    (pipeline / "nets.base.json").write_text('{"nets": "base"}')
    (pipeline / "nets.sensor.json").write_text('{"nets": "sensor"}')

    evidence, included = review_panel.build_evidence(pipeline, "sensor")
    assert '"sensor"' in evidence and '"base"' not in evidence
    assert "nets.json" in included   # the logical name, not the file on disk

    plain, _ = review_panel.build_evidence(pipeline, None)
    assert "nets.base.json" not in plain
