"""The autoroute pipeline step: every run leaves a report, and the board is
snapshotted before anything touches it.

The Freerouting jar, java and pcbnew are all stubbed. What is under test is
BLPL's contract with Stage 8 — that `autoroute_report.json` exists after every
outcome and says whether a router actually ran — not Freerouting itself. (The
real round trip was exercised by hand against Freerouting 2.4.1 / KiCad 10.0.5;
see the module docstring.)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from blpl.core import autoroute


def _report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _make_available(monkeypatch, tmp_path: Path) -> Path:
    jar = tmp_path / "freerouting.jar"
    jar.write_bytes(b"")
    monkeypatch.setattr(autoroute, "find_kicad_cli", lambda: "/usr/bin/kicad-cli")
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(autoroute, "find_jar", lambda: jar)
    monkeypatch.setattr(autoroute.shutil, "which", lambda name: "/usr/bin/java")
    return jar


def _board(tmp_path: Path) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20260206))", encoding="utf-8")
    return pcb


@pytest.fixture
def unavailable(monkeypatch):
    monkeypatch.setattr(autoroute, "find_kicad_cli", lambda: None)
    monkeypatch.setattr(autoroute, "find_pcbnew_python", lambda: None)
    monkeypatch.setattr(autoroute, "find_jar", lambda: None)
    monkeypatch.setattr(autoroute.shutil, "which", lambda _: None)


def test_a_skipped_run_still_writes_a_report_naming_what_is_missing(unavailable, tmp_path) -> None:
    pcb = _board(tmp_path)
    report_path = tmp_path / ".pipeline" / "autoroute_report.json"

    result = autoroute.run_for(pcb, report_path)

    assert result.attempted is False and result.ok is False
    rep = _report(report_path)
    assert rep["attempted"] is False
    for needed in ("kicad-cli", "pcbnew", "java", autoroute.JAR_ENV):
        assert needed in rep["reason"], needed
    assert rep["finished_at"]
    # Nothing ran, so nothing was snapshotted or exported.
    assert rep["snapshot"] == "" and rep["dsn"] == "" and rep["ses"] == ""


def test_a_missing_board_is_not_attempted(monkeypatch, tmp_path) -> None:
    _make_available(monkeypatch, tmp_path)
    report_path = tmp_path / "autoroute_report.json"
    result = autoroute.run_for(tmp_path / "absent.kicad_pcb", report_path)
    assert result.attempted is False
    assert "absent.kicad_pcb" in _report(report_path)["reason"]


def _fake_runs(monkeypatch, *, dsn_ok=True, ses_ok=True, import_ok=True,
               summary="(2 unrouted and 1 violations)",
               lands=True, measured_unconnected=0):
    """Stand in for pcbnew export, Freerouting, and pcbnew import.

    Tells them apart by the command shape: `python -c` code that mentions
    ExportSpecctraDSN or ImportSpecctraSES, and `java -jar` for the router.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=300):
        calls.append(cmd)
        if "ExportSpecctraDSN" in (cmd[2] if len(cmd) > 2 else ""):
            if dsn_ok:
                dsn = Path(cmd[2].split("ExportSpecctraDSN(b, '")[1].split("'")[0])
                dsn.write_text("(pcb)")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 1, "", "no board")
        if cmd[0] == "java":
            out = f"INFO Auto-routing stage completed: started with 3 unrouted nets {summary}"
            if ses_ok:
                Path(cmd[cmd.index("-do") + 1]).write_text("(session)")
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if "ImportSpecctraSES" in (cmd[2] if len(cmd) > 2 else ""):
            if import_ok and lands:
                # A real import writes tracks into the board. `lands=False`
                # reproduces the case this check exists for: the call returns
                # success and nothing reaches the file.
                board = Path(cmd[2].split("LoadBoard(")[1].split("'")[1])
                board.write_text(
                    board.read_text()
                    + "\n  (segment (start 0 0) (end 1 0) (width 0.2) (layer \"F.Cu\") (net 1))"
                )
            return subprocess.CompletedProcess(cmd, 0 if import_ok else 1, "", "" if import_ok else "bad ses")
        if "BuildConnectivity" in (cmd[2] if len(cmd) > 2 else ""):
            return subprocess.CompletedProcess(
                cmd, 0,
                'BLPL_MEASURE {"unconnected": %d, "nets": 7}' % measured_unconnected, "")
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(autoroute, "_run", fake_run)
    return calls


def test_a_successful_run_snapshots_first_and_records_the_routers_verdict(monkeypatch, tmp_path) -> None:
    jar = _make_available(monkeypatch, tmp_path)
    calls = _fake_runs(monkeypatch)
    pcb = _board(tmp_path)
    original = pcb.read_text()
    report_path = tmp_path / ".pipeline" / "autoroute_report.json"

    result = autoroute.run_for(pcb, report_path, passes=3)

    assert result.ok and result.attempted
    rep = _report(report_path)
    assert rep["ok"] is True and rep["attempted"] is True
    assert rep["passes"] == 3 and rep["jar"] == str(jar)
    # `unrouted` is the board's number now, not the router's claim about its own
    # copy. Both are kept, and they are different fields precisely so a
    # disagreement between them is visible rather than averaged away.
    assert rep["unrouted"] == 0
    assert rep["router_reported_unrouted"] == 2 and rep["violations"] == 1
    assert rep["segments"] == 1
    # The snapshot is the board as it was before the router touched it.
    snap = Path(rep["snapshot"])
    assert snap.is_file() and snap.read_text() == original
    assert snap.parent == report_path.parent / "autoroute"
    assert Path(rep["dsn"]).is_file() and Path(rep["ses"]).is_file()
    # Freerouting was invoked headless, with the flags v2.4.1 documents, and
    # without the design-rules file that nothing ever wrote.
    java = next(c for c in calls if c[0] == "java")
    assert "-jar" in java and str(jar) in java
    assert java[java.index("-mp") + 1] == "3"
    assert "--gui.enabled=false" in java and "-da" in java
    assert "-dr" not in java
    # Order: export, route, import, then ask the board what it got.
    def kind(c):
        if c[0] == "java":
            return "java"
        if "Export" in c[2]:
            return "export"
        if "Import" in c[2]:
            return "import"
        return "measure"
    assert [kind(c) for c in calls] == ["export", "java", "import", "measure"]


def test_export_failure_is_reported_and_leaves_the_board_alone(monkeypatch, tmp_path) -> None:
    _make_available(monkeypatch, tmp_path)
    _fake_runs(monkeypatch, dsn_ok=False)
    pcb = _board(tmp_path)
    report_path = tmp_path / "autoroute_report.json"

    result = autoroute.run_for(pcb, report_path)

    assert result.attempted and not result.ok
    rep = _report(report_path)
    assert "Specctra export failed" in rep["reason"] and "no board" in rep["reason"]
    assert rep["ses"] == ""
    assert Path(rep["snapshot"]).is_file()


def test_no_session_file_is_reported(monkeypatch, tmp_path) -> None:
    _make_available(monkeypatch, tmp_path)
    _fake_runs(monkeypatch, ses_ok=False)
    report_path = tmp_path / "autoroute_report.json"

    result = autoroute.run_for(_board(tmp_path), report_path)

    assert result.attempted and not result.ok
    rep = _report(report_path)
    assert "no session file" in rep["reason"]
    assert rep["dsn"] and rep["ses"] == ""


def test_import_failure_is_reported_with_the_session_kept(monkeypatch, tmp_path) -> None:
    _make_available(monkeypatch, tmp_path)
    _fake_runs(monkeypatch, import_ok=False)
    report_path = tmp_path / "autoroute_report.json"

    result = autoroute.run_for(_board(tmp_path), report_path)

    assert result.attempted and not result.ok
    rep = _report(report_path)
    assert "could not be imported" in rep["reason"] and "bad ses" in rep["reason"]
    # The SES is kept so someone can import it by hand.
    assert Path(rep["ses"]).is_file()


def test_route_alone_does_not_snapshot(monkeypatch, tmp_path) -> None:
    """`route` is the bare round trip the agent tool calls; `run_for` owns the
    snapshot and the report."""
    _make_available(monkeypatch, tmp_path)
    _fake_runs(monkeypatch)
    pcb = _board(tmp_path)
    result = autoroute.route(pcb, work_dir=tmp_path / "w")
    assert result.ok and result.snapshot == ""
    assert not list(tmp_path.glob("**/*.pre-autoroute.kicad_pcb"))


def test_to_dict_keeps_the_fields_the_agent_tool_reads() -> None:
    d = autoroute.RouteResult(False, "why").to_dict()
    for key in ("ok", "reason", "dsn", "ses", "log", "attempted"):
        assert key in d


# --- the result is measured, not believed ----------------------------------
#
# Freerouting reports what it achieved on its own copy of the board. That
# number used to be stored as the run's result, so a session file that never
# landed still produced a report saying every connection was routed. On one
# project every board read `unrouted: 0` while carrying zero track segments,
# and nothing in the pipeline could tell.


def test_a_route_that_does_not_reach_the_board_is_a_failure(monkeypatch, tmp_path) -> None:
    """ImportSpecctraSES returning success is not evidence that anything landed."""
    _make_available(monkeypatch, tmp_path)
    _fake_runs(monkeypatch, lands=False, summary="(0 unrouted and 0 violations)")
    pcb = _board(tmp_path)
    report_path = tmp_path / "autoroute_report.json"

    result = autoroute.run_for(pcb, report_path)

    assert result.ok is False, "reported success for a board with no tracks"
    rep = _report(report_path)
    assert rep["segments"] == 0
    # The router's own claim is kept, and it is the thing that disagrees.
    assert rep["router_reported_unrouted"] == 0
    assert "no track segments" in rep["reason"]


def test_the_boards_number_wins_when_it_disagrees_with_the_router(monkeypatch, tmp_path) -> None:
    """The router says it finished; the board still has open connections.

    Stage 8 reads `unrouted`, so this is the field that has to be true — an
    unrouted connection is a finding about the design, and it must not be
    hidden by a summary line from a different copy of the board.
    """
    _make_available(monkeypatch, tmp_path)
    _fake_runs(monkeypatch, summary="(0 unrouted and 0 violations)",
               measured_unconnected=5)
    pcb = _board(tmp_path)
    report_path = tmp_path / "autoroute_report.json"

    result = autoroute.run_for(pcb, report_path)

    assert result.ok is True          # tracks did land; it is simply not finished
    rep = _report(report_path)
    assert rep["unrouted"] == 5, "believed the router over the board"
    assert rep["router_reported_unrouted"] == 0


def test_count_segments_reads_the_file_not_the_broken_binding(tmp_path) -> None:
    """pcbnew's GetTracks() raises on current Python — it calls it.next(), the
    Python 2 spelling — so the count comes from the board text instead."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb\n"
        '  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1))\n'
        '  (segment (start 1 0) (end 2 0) (width 0.2) (layer "F.Cu") (net 1))\n'
        '  (via (at 1 0) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))\n'
        ")\n"
    )
    assert autoroute.count_segments(pcb) == 2
    assert autoroute.count_segments(tmp_path / "absent.kicad_pcb") == 0
