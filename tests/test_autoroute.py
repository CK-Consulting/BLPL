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
               summary="(2 unrouted and 1 violations)"):
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
            return subprocess.CompletedProcess(cmd, 0 if import_ok else 1, "", "" if import_ok else "bad ses")
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
    assert rep["unrouted"] == 2 and rep["violations"] == 1
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
    # Order: export, route, import.
    kinds = ["java" if c[0] == "java" else "export" if "Export" in c[2] else "import"
             for c in calls]
    assert kinds == ["export", "java", "import"]


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
