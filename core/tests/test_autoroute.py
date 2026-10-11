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
                dsn.write_text('(pcb "d" (board))')
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
    # Connections still open: zero segments here means the session was lost,
    # not that the board had nothing to route.
    _fake_runs(monkeypatch, lands=False, summary="(0 unrouted and 0 violations)",
               measured_unconnected=3)
    pcb = _board(tmp_path)
    report_path = tmp_path / "autoroute_report.json"

    result = autoroute.run_for(pcb, report_path)

    assert result.ok is False, "reported success for a board with no tracks"
    rep = _report(report_path)
    assert rep["segments"] == 0 and rep["unrouted"] == 3
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


# --- routing that survives a re-emit ----------------------------------------
#
# Stage 6 writes a new timestamped board every run and routing lives in that
# file, so re-emitting after editing a comment threw away a finished route. The
# only symptom was a board that opened full of ratsnest, which reads as "the
# autorouter does not work" rather than "nothing carried the route across".


def _carry_stub(monkeypatch, *, import_ok=True, segments=42):
    """Export a DSN that mirrors the board it came from.

    That is what makes the comparison meaningful here: two exports are equal
    exactly when the two boards are, which is the property carry_forward relies
    on to decide the old session still fits.
    """
    calls: list[str] = []

    def fake_run(cmd, timeout=300):
        code = cmd[2] if len(cmd) > 2 else ""
        if cmd[0] == "java":
            calls.append("java")
            Path(cmd[cmd.index("-do") + 1]).write_text("(session)")
            return subprocess.CompletedProcess(
                cmd, 0, "INFO Auto-routing stage completed: (0 unrouted and 0 violations)", "")
        if "ExportSpecctraDSN" in code:
            calls.append("export")
            src = Path(code.split("LoadBoard(")[1].split("'")[1])
            dst = Path(code.split("ExportSpecctraDSN(b, '")[1].split("'")[0])
            dst.write_text(f'(pcb "{dst}" {src.read_text()})')
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "ImportSpecctraSES" in code:
            calls.append("import")
            if import_ok:
                board = Path(code.split("LoadBoard(")[1].split("'")[1])
                board.write_text(board.read_text() + "\n" + "\n".join(
                    "  (segment (start 0 0) (end 1 0) (width 0.2) (layer \"F.Cu\") (net 1))"
                    for _ in range(segments)))
            return subprocess.CompletedProcess(cmd, 0 if import_ok else 1, "", "")
        if "BuildConnectivity" in code:
            calls.append("measure")
            return subprocess.CompletedProcess(cmd, 0, 'BLPL_MEASURE {"unconnected": 0}', "")
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(autoroute, "_run", fake_run)
    return calls


def _prior_run(work: Path, board_text: str, base: str = "brd_2026-01-01_000000Z") -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / f"{base}.pre-autoroute.kicad_pcb").write_text(board_text)
    (work / f"{base}.ses").write_text("(session)")


def test_an_unchanged_board_reuses_the_last_route_without_running_the_router(
    monkeypatch, tmp_path
) -> None:
    _make_available(monkeypatch, tmp_path)
    calls = _carry_stub(monkeypatch)
    work = tmp_path / ".pipeline" / "autoroute"
    text = "(kicad_pcb (version 20260206) (footprint A) (footprint B))"
    _prior_run(work, text)

    pcb = tmp_path / "board.kicad_pcb"          # re-emitted, same content
    pcb.write_text(text)
    result = autoroute.run_for(pcb, tmp_path / "r.json", work_dir=work)

    assert result.ok and result.segments == 42
    assert "java" not in calls, "re-ran the router for a board that had not changed"
    rep = _report(tmp_path / "r.json")
    assert rep["carried_from"].endswith(".ses")
    assert rep["passes"] == 0


def test_a_board_that_moved_is_routed_again_rather_than_carried(monkeypatch, tmp_path) -> None:
    """The safety half: an old session's coordinates are only valid for the
    placement they were produced from."""
    _make_available(monkeypatch, tmp_path)
    calls = _carry_stub(monkeypatch)
    work = tmp_path / ".pipeline" / "autoroute"
    _prior_run(work, "(kicad_pcb (footprint A) (footprint B))")

    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (footprint A) (footprint B) (footprint C))")   # a part moved in
    result = autoroute.run_for(pcb, tmp_path / "r.json", work_dir=work)

    assert "java" in calls, "carried a session onto a board whose placement changed"
    assert result.ok
    assert "carried_from" not in _report(tmp_path / "r.json")


def test_carry_is_skipped_when_there_is_nothing_to_carry(monkeypatch, tmp_path) -> None:
    _make_available(monkeypatch, tmp_path)
    calls = _carry_stub(monkeypatch)
    pcb = _board(tmp_path)
    autoroute.run_for(pcb, tmp_path / "r.json", work_dir=tmp_path / "w")
    assert "java" in calls


def test_the_pre_route_snapshot_is_never_overwritten(monkeypatch, tmp_path) -> None:
    """Routing a board twice must not replace the record of what it looked
    like before the first run. Otherwise "undo the autoroute" restores a routed
    board, and carry_forward compares against something that already has
    routing in it — both of which happened on a real board whose pre-autoroute
    snapshot held 219 track segments."""
    _make_available(monkeypatch, tmp_path)
    _carry_stub(monkeypatch)
    work = tmp_path / "w"
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (footprint A))")
    original = pcb.read_text()

    autoroute.run_for(pcb, tmp_path / "r.json", work_dir=work)
    snap = work / "board.pre-autoroute.kicad_pcb"
    assert snap.read_text() == original

    # Second run: the board now carries routing. The snapshot must not follow.
    autoroute.run_for(pcb, tmp_path / "r2.json", work_dir=work)
    assert snap.read_text() == original, "snapshot was overwritten with the routed board"
    assert autoroute.count_segments(snap) == 0


def test_the_snapshot_carries_its_project_file(monkeypatch, tmp_path) -> None:
    """Net classes live in `.kicad_pro`, and pcbnew reads the one beside the
    board. A snapshot without it is a different board: every class falls back
    to defaults. That made two exports of the same design differ by nine lines
    of net-class rules and stopped carry_forward from ever matching."""
    _make_available(monkeypatch, tmp_path)
    _carry_stub(monkeypatch)
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (footprint A))")
    pro = tmp_path / "board.kicad_pro"
    pro.write_text('{"net_settings": {"classes": ["Power_Bulk"]}}')
    work = tmp_path / "w"

    autoroute.run_for(pcb, tmp_path / "r.json", work_dir=work)

    carried = work / "board.pre-autoroute.kicad_pro"
    assert carried.is_file(), "snapshot left its net classes behind"
    assert carried.read_text() == pro.read_text()


# --- the comparison has to keep the structure -------------------------------


def test_swapping_pins_between_nets_is_not_equal() -> None:
    """Sorting the DSN's lines threw away which net each pin list belonged to.

    Two boards with the pin lists of GND and VCC exchanged produced the same
    sorted multiset and compared equal, so carry_forward would have imported a
    session onto a board whose connectivity had changed underneath it.
    """
    a = '(pcb "a" (network (net GND (pins U1-1 U1-2)) (net VCC (pins U2-1 U2-2))))'
    b = '(pcb "b" (network (net GND (pins U2-1 U2-2)) (net VCC (pins U1-1 U1-2))))'
    assert autoroute._dsn_body(a) != autoroute._dsn_body(b)


def test_the_filename_and_child_order_are_still_normalised_away() -> None:
    """The two things that legitimately differ between exports of one board:
    the timestamped path it was written from, and the order pcbnew happened to
    emit its children in."""
    a = '(pcb "one.dsn" (placement (component A (place R1 1 2)) (component B (place R2 3 4))))'
    b = '(pcb "two.dsn" (placement (component B (place R2 3 4)) (component A (place R1 1 2))))'
    assert autoroute._dsn_body(a) == autoroute._dsn_body(b)


def test_coordinate_order_inside_a_path_is_preserved() -> None:
    """Atoms are a geometry list — reordering them would change the track."""
    a = '(pcb "x" (wire (path F.Cu 200 0 0 10 10)))'
    b = '(pcb "x" (wire (path F.Cu 200 10 10 0 0)))'
    assert autoroute._dsn_body(a) != autoroute._dsn_body(b)


# --- a board with nothing to route is not a failure -------------------------


def test_a_board_with_nothing_to_route_succeeds_with_no_tracks(monkeypatch, tmp_path) -> None:
    """A mechanical board, or one whose nets are all single-pad, legitimately
    needs no track at all. An unconditional "no segments means failure" check
    made `blpl autoroute` exit 1 on a board that was already complete."""
    _make_available(monkeypatch, tmp_path)
    _fake_runs(monkeypatch, lands=False, measured_unconnected=0)
    pcb = _board(tmp_path)

    result = autoroute.run_for(pcb, tmp_path / "r.json")

    assert result.ok is True
    rep = _report(tmp_path / "r.json")
    assert rep["segments"] == 0 and rep["unrouted"] == 0 and rep["reason"] == ""


def test_a_route_is_refused_when_the_board_cannot_be_measured(monkeypatch, tmp_path) -> None:
    """Without a board-derived number there is no evidence the routing landed,
    and accepting the run would be the original bug in a new hat: `unrouted`
    would be None and nothing would notice."""
    _make_available(monkeypatch, tmp_path)
    calls = _fake_runs(monkeypatch)

    real_run = autoroute._run

    def no_measure(cmd, timeout=300):
        if len(cmd) > 2 and "BuildConnectivity" in cmd[2]:
            return subprocess.CompletedProcess(cmd, 1, "", "pcbnew exploded")
        return real_run(cmd, timeout)

    monkeypatch.setattr(autoroute, "_run", no_measure)
    result = autoroute.run_for(_board(tmp_path), tmp_path / "r.json")

    assert result.ok is False
    assert "could not be measured" in _report(tmp_path / "r.json")["reason"]
