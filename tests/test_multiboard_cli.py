"""The command line on a multi-board project.

These cover three ways the board can go missing between the flag and the file
it decides. Each one produces a plausible-looking result rather than an error,
which is why they get tests rather than a comment.
"""

from __future__ import annotations

import argparse

import pytest

from blpl.core import cli

MANIFEST = """
## Boards

- base
- sensor (optional)

## Configurations

- full: base, sensor
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "project.md").write_text(MANIFEST)
    for board in ("base", "sensor"):
        (tmp_path / board).mkdir()
        (tmp_path / board / "design.md").write_text(f"# {board}\n")
    return tmp_path


def test_run_forwards_the_board_to_every_stage(project):
    """`run` parses --board, builds a namespace for each stage, and used to
    leave the board out of it — so `run --board base` reached stage0 with no
    board and died on 'has more than one board'. The flag existed and the
    full-pipeline command was unusable on every multi-board project."""
    seen = []
    ns = argparse.Namespace(
        project_dir=str(project), llm_provider=None, llm_model=None, board="base",
        start_stage="stage0", end_stage="stage0", stage0_mode="det",
        continue_on_error=False,
    )

    def spy(args):
        seen.append(cli._board(args, project))
        return 0

    original = cli._cmd_stage0_det
    cli._cmd_stage0_det = spy
    try:
        assert cli._cmd_run(ns) == 0
    finally:
        cli._cmd_stage0_det = original
    assert seen == ["base"]


def test_a_single_board_project_still_needs_no_flag(tmp_path):
    (tmp_path / "design.md").write_text("# one board\n")
    seen = []
    ns = argparse.Namespace(
        project_dir=str(tmp_path), llm_provider=None, llm_model=None, board=None,
        start_stage="stage0", end_stage="stage0", stage0_mode="det",
        continue_on_error=False,
    )
    original = cli._cmd_stage0_det
    cli._cmd_stage0_det = lambda args: (seen.append(cli._board(args, tmp_path)), 0)[1]
    try:
        assert cli._cmd_run(ns) == 0
    finally:
        cli._cmd_stage0_det = original
    assert seen == [None]


def test_stage7_validates_the_board_it_was_asked_about(project):
    """Stage 7 picked the newest emitted PCB regardless of board. Running it on
    `sensor` right after compiling `base` therefore reported on `base` and
    filed the result under `sensor` — a clean report for a board nobody
    checked, which is worse than no report."""
    pipeline = project / ".pipeline"
    pipeline.mkdir()
    (pipeline / "p_base_2026-08-19_120000Z.kicad_pcb").write_text("base pcb")
    (pipeline / "p_base_2026-08-19_120000Z.kicad_sch").write_text("base sch")
    # sensor compiled *earlier*, so "newest" is the wrong answer for it.
    (pipeline / "p_sensor_2026-08-19_010000Z.kicad_pcb").write_text("sensor pcb")
    (pipeline / "p_sensor_2026-08-19_010000Z.kicad_sch").write_text("sensor sch")

    captured = {}

    from blpl.core import stage7_validate

    original = stage7_validate.run

    def spy(proj, *, pcb_path=None, sch_path=None, **kw):
        captured["pcb"] = pcb_path
        captured["sch"] = sch_path
        raise SystemExit(0)

    stage7_validate.run = spy
    try:
        with pytest.raises(SystemExit):
            cli._cmd_stage7(
                argparse.Namespace(project_dir=str(project), board="sensor")
            )
    finally:
        stage7_validate.run = original

    assert "sensor" in captured["pcb"].name
    assert "sensor" in captured["sch"].name


def _minimal_hdm() -> dict:
    return {
        "project": {
            "name": "Shield",
            "board_id": "SH-1",
            "dimensions": [50, 40],
            "stackup": {"layers": 2, "thickness": 1.6, "finish": "HASL"},
        },
        "net_classes": {
            "Default": {"trace_width": 0.2, "clearance": 0.15, "via_dia": 0.6, "via_drill": 0.3}
        },
        "components": {},
        "nets": {},
    }


def test_two_boards_do_not_compile_over_each_other(project):
    """Stage 6 named its output from the HDM project name and a timestamp
    alone, so both boards of one project produced the same stem — and two
    compiles inside the same second meant the second silently replaced the
    first. The board also has to appear *in* the stem, because that is how the
    design route tells one board's emitted files from another's."""
    import yaml

    from blpl.core import stage6_compile_kicad

    hdm = project / "hdm.yaml"
    hdm.write_text(yaml.safe_dump(_minimal_hdm()))
    out = project / ".pipeline"

    stamp = "2026-08-19_120000Z"       # same second for both, which is the point
    base = stage6_compile_kicad.run(hdm, out, project_dir=project, board="base", stamp=stamp)
    sensor = stage6_compile_kicad.run(hdm, out, project_dir=project, board="sensor", stamp=stamp)

    assert base["pcb"] != sensor["pcb"]
    assert base["pcb"].is_file() and sensor["pcb"].is_file()
    # `_latest_with_origin` filters on this exact shape.
    assert "_base_" in base["pcb"].name
    assert "_sensor_" in sensor["pcb"].name


def test_a_single_board_project_keeps_its_old_filenames(project):
    """Names already on disk and in git must not move because boards exist."""
    import yaml

    from blpl.core import stage6_compile_kicad

    hdm = project / "hdm.yaml"
    hdm.write_text(yaml.safe_dump(_minimal_hdm()))
    out = stage6_compile_kicad.run(
        hdm, project / ".pipeline", project_dir=project, board=None, stamp="2026-08-19_120000Z"
    )
    assert out["pcb"].name == "Shield_2026-08-19_120000Z.kicad_pcb"
