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


def test_init_writes_each_boards_config_beside_its_own_markdown(project, monkeypatch):
    """Two boards do not share an outline or a stackup; a project.yaml at the
    root would let the second board silently describe the first."""
    for board in ("base", "sensor"):
        rc = cli.main(["init", "--project-dir", str(project), "--board", board])
        assert rc == 0
        assert (project / board / "project.yaml").is_file()
    assert not (project / "project.yaml").exists()


def test_autoroute_without_a_router_writes_the_reason_and_does_not_fail(project, monkeypatch):
    from blpl.core import autoroute

    monkeypatch.setattr(autoroute, "available", lambda: (False, "bulk autorouting needs the Freerouting jar"))
    (project / ".pipeline").mkdir()
    (project / ".pipeline" / "demo_base_2026.kicad_pcb").write_text("(kicad_pcb)")
    rc = cli.main(["autoroute", "--project-dir", str(project), "--board", "base"])
    assert rc == 0
    import json
    report = json.loads((project / ".pipeline" / "autoroute_report.base.json").read_text())
    assert report["attempted"] is False
    assert "Freerouting jar" in report["reason"]


def test_autoroute_needs_a_compiled_board(project):
    assert cli.main(["autoroute", "--project-dir", str(project), "--board", "base"]) == 2


def test_crossboard_command_checks_the_declared_mates(tmp_path, capsys):
    (tmp_path / "project.md").write_text(
        "## Boards\n\n- base\n- sensor (optional)\n\n## Mates\n\n- base.J3 <-> sensor.J1\n"
    )
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "d.md").write_text(
        "## J3 pinout\n\n| Pin | Signal |\n|---|---|\n| 1 | SDA |\n| 2 | SCL |\n"
    )
    (tmp_path / "sensor").mkdir()
    (tmp_path / "sensor" / "d.md").write_text(
        "## J1 pinout\n\n| Pin | Signal |\n|---|---|\n| 1 | SCL |\n| 2 | SDA |\n"
    )
    for b in ("base", "sensor"):
        assert cli.main(["stage0-det", "--project-dir", str(tmp_path), "--board", b]) == 0
    rc = cli.main(["crossboard", "--project-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "BLOCKED" in out and "signal_mismatch" in out
    assert (tmp_path / ".pipeline" / "crossboard.json").is_file()


def test_crossboard_on_a_single_board_project_has_nothing_to_say(tmp_path, capsys):
    (tmp_path / "d.md").write_text("# one\n")
    assert cli.main(["crossboard", "--project-dir", str(tmp_path)]) == 0
    assert "single-board" in capsys.readouterr().out


def test_run_all_boards_runs_each_board_then_the_crossboard_check(project, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cli, "_cmd_stage0_det", lambda ns: seen.append(f"stage0:{ns.board}") or 0)
    monkeypatch.setattr(cli, "_cmd_crossboard", lambda ns: seen.append("crossboard") or 0)
    rc = cli.main(["run", "--project-dir", str(project), "--board", "all", "--to", "stage0"])
    assert rc == 0
    assert seen == ["stage0:base", "stage0:sensor", "crossboard"]


def test_run_all_boards_is_refused_on_a_single_board_project(tmp_path):
    (tmp_path / "d.md").write_text("# one\n")
    assert cli.main(["run", "--project-dir", str(tmp_path), "--board", "all", "--to", "stage0"]) == 2


def test_run_routes_after_stage6_unless_told_not_to(project, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cli, "_cmd_stage6", lambda ns: seen.append("stage6") or 0)
    monkeypatch.setattr(cli, "_cmd_autoroute", lambda ns: seen.append(f"autoroute:{ns.passes}") or 0)
    args = ["run", "--project-dir", str(project), "--board", "base", "--from", "stage6", "--to", "stage6"]
    assert cli.main([*args, "--passes", "3"]) == 0
    assert seen == ["stage6", "autoroute:3"]
    seen.clear()
    assert cli.main([*args, "--no-autoroute"]) == 0
    assert seen == ["stage6"]


def test_stage8_reviews_the_board_it_was_asked_about(project, monkeypatch):
    from blpl.core import stage8_review

    pipeline = project / ".pipeline"
    pipeline.mkdir()
    (pipeline / "demo_base_2026-01-01.kicad_sch").write_text("(kicad_sch)")
    (pipeline / "demo_sensor_2026-01-02.kicad_sch").write_text("(kicad_sch)")
    got = {}

    def _fake(proj, **kw):
        got.update(kw)
        return {"skipped": True, "reason": "stubbed"}

    monkeypatch.setattr(stage8_review, "run", _fake)
    assert cli.main(["stage8", "--project-dir", str(project), "--board", "base", "--no-lifecycle"]) == 0
    assert got["board"] == "base"
    assert got["sch_path"].name == "demo_base_2026-01-01.kicad_sch"
    assert got["lifecycle"] is False
