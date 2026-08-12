"""Unit tests for blpl.agent.tools.spice and its Stage 8 integration.

No simulator is *assumed* to exist — ngspice is a third-party install — so the
bulk of these stub the probe or assert on the skip path. That is not a
concession: the skip path is what most users hit, and it is the one that must
never be silent.

The end of the file adds the other half. When ngspice really is installed, a
handful of tests drive the whole chain — detect, generate a testbench, run the
simulator, parse, evaluate — against an RC low-pass whose cutoff is known
analytically. Stubs can only prove the code agrees with itself; these prove the
numbers are right, and that a wrong filter is actually *caught* rather than
merely reported on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blpl.agent.tools import spice
from blpl.core import stage8_review as s8


def _report(*results: dict, findings: list[dict] | None = None) -> dict:
    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
    for r in results:
        counts[r.get("status", "skip")] += 1
    return {
        "analyzer_type": "spice",
        "summary": {"total": len(results), **counts},
        "findings": findings or [],
        "simulation_results": list(results),
    }


def _sim_result(status: str = "pass", **kw) -> dict:
    return {
        "subcircuit_type": "rc_filter",
        "components": ["R5", "C3"],
        "status": status,
        "expected": {"fc_hz": 1000.0},
        "simulated": {"fc_hz": 1012.0},
        "delta": {"fc_hz": 1.2},
        **kw,
    }


# ---------------------------------------------------------------------------
# The skip paths
# ---------------------------------------------------------------------------


def test_missing_schematic_analysis_is_a_named_skip(tmp_path: Path) -> None:
    run = spice.simulate(tmp_path / "nope.json", tmp_path / "out.json")
    assert run.skipped is True
    # ok stays True: nothing broke, there was simply nothing to simulate.
    assert run.ok is True
    assert "stage8" in run.reason
    assert not (tmp_path / "out.json").exists()


def test_absent_simulator_skips_with_an_install_hint(tmp_path: Path, monkeypatch) -> None:
    sch = tmp_path / "schematic.json"
    sch.write_text(json.dumps({"findings": []}))
    monkeypatch.setattr(
        spice, "find_simulator", lambda *_a, **_k: spice.SimulatorStatus(False, detail="none here")
    )
    run = spice.simulate(sch, tmp_path / "out.json")
    assert run.skipped is True
    assert run.reason == "none here"


def test_the_install_hint_names_every_supported_simulator() -> None:
    # A skip whose reason is "not found" and nothing else is a dead end for the
    # reader; the way out has to travel with the refusal.
    for name in ("ngspice", "LTspice", "Xyce", "NGSPICE_PATH"):
        assert name in spice._INSTALL_HINT


def test_missing_kicad_happy_is_reported_not_raised(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise spice.KicadHappyMissing("kicad-happy not found")

    monkeypatch.setattr(spice, "script_path", _boom)
    status = spice.find_simulator()
    assert status.available is False
    assert "kicad-happy not found" in status.detail


def test_headline_states_the_skip_reason() -> None:
    run = spice.SimRun(ok=True, skipped=True, reason="no simulator")
    assert run.headline() == "skipped — no simulator"


# ---------------------------------------------------------------------------
# Parasitics: present only on a routed board
# ---------------------------------------------------------------------------


def test_no_pcb_analysis_means_ideal_nets(tmp_path: Path) -> None:
    path, note = spice.extract_parasitics(tmp_path / "absent.json", tmp_path / "p.json")
    assert path is None
    assert "ideal nets" in note


def test_unrouted_board_yields_no_parasitics(tmp_path: Path) -> None:
    """The normal state of a freshly-emitted BLPL board: copper exists in the
    file but no traces are drawn, so there is nothing to extract."""
    pcb = tmp_path / "pcb.json"
    pcb.write_text(json.dumps({"net_lengths": [{"net": "VCC_3V3", "length_mm": 0}]}))
    path, note = spice.extract_parasitics(pcb, tmp_path / "p.json")
    assert path is None
    assert "no routed traces" in note


def test_unreadable_pcb_analysis_degrades_rather_than_raises(tmp_path: Path) -> None:
    pcb = tmp_path / "pcb.json"
    pcb.write_text("{not json")
    path, note = spice.extract_parasitics(pcb, tmp_path / "p.json")
    assert path is None
    assert "unreadable" in note


# ---------------------------------------------------------------------------
# Summarising a real report
# ---------------------------------------------------------------------------


def test_summary_flattens_counts_and_results() -> None:
    run = spice._summarise(
        _report(_sim_result("pass"), _sim_result("fail"), _sim_result("skip")),
        simulator="ngspice",
    )
    assert run.ok is True
    assert run.counts == {"total": 3, "pass": 1, "warn": 0, "fail": 1, "skip": 1}
    assert run.results[0].reference == "R5/C3"
    assert run.results[1].status == "fail"


def test_a_failing_simulation_is_still_a_successful_run() -> None:
    """`ok` tracks whether the simulator ran, not whether the circuit passed.

    Collapsing the two would report a working tool as broken, and hide which of
    "the filter is wrong" and "nothing was measured" actually happened.
    """
    run = spice._summarise(_report(_sim_result("fail")), simulator="ngspice")
    assert run.ok is True
    assert run.counts["fail"] == 1


def test_headline_says_whether_parasitics_were_included() -> None:
    report = _report(_sim_result("pass"))
    ideal = spice._summarise(report, simulator="ngspice", parasitics=False)
    routed = spice._summarise(report, simulator="ngspice", parasitics=True)
    assert "ideal nets" in ideal.headline()
    assert "with PCB parasitics" in routed.headline()


def test_an_empty_run_is_not_reported_as_a_pass() -> None:
    """dev.04's only candidate is a crystal drawn without load caps, so the
    simulator runs and builds nothing. "0 pass, 0 fail" would read as a clean
    bill of health for a board where nothing was actually checked."""
    run = spice._summarise(_report(), simulator="ltspice")
    assert run.ok is True
    assert run.skipped is False
    assert run.nothing_to_simulate is True
    assert "nothing was verified" in run.headline()


def test_a_run_with_results_is_not_flagged_as_empty() -> None:
    run = spice._summarise(_report(_sim_result("pass")), simulator="ngspice")
    assert run.nothing_to_simulate is False


def test_a_skip_is_not_confused_with_an_empty_run() -> None:
    # Three distinct states, three distinct reports: nobody looked, nothing was
    # simulatable, everything passed.
    assert spice.SimRun(ok=True, skipped=True, reason="x").nothing_to_simulate is False


def test_all_skipped_is_not_reported_as_a_pass() -> None:
    """Verified against LTspice 24 on macOS: it runs the sweep and writes a
    valid .raw, but kicad-happy reads only ngspice-style measurements, so every
    result is a skip. "0 fail" must not be allowed to read as "all good"."""
    run = spice._summarise(
        _report(_sim_result("skip"), _sim_result("skip")), simulator="ltspice"
    )
    assert run.nothing_measured is True
    assert "nothing was verified" in run.headline()
    assert "install ngspice" in run.headline()


def test_a_mixed_run_is_not_flagged_as_unmeasured() -> None:
    run = spice._summarise(_report(_sim_result("pass"), _sim_result("skip")))
    assert run.nothing_measured is False


def test_skip_notes_are_carried_through() -> None:
    run = spice._summarise(
        _report(_sim_result("skip", note="ngspice could not measure -3dB frequency"))
    )
    assert run.results[0].note == "ngspice could not measure -3dB frequency"


def test_findings_survive_into_the_run() -> None:
    finding = {"rule_id": "SP-FAIL", "severity": "error", "summary": "rc_filter R5/C3 missed fc"}
    run = spice._summarise(_report(_sim_result("fail"), findings=[finding]))
    assert run.findings == [finding]


# ---------------------------------------------------------------------------
# Stage 8 integration
# ---------------------------------------------------------------------------


def _stage8_with_spice(tmp_path: Path, monkeypatch, spice_report: dict | None, **kw) -> dict:
    """Run Stage 8 with stubbed analyzers and a stubbed simulation."""
    pipeline = tmp_path / ".pipeline"
    pipeline.mkdir(parents=True, exist_ok=True)
    (pipeline / "bom.json").write_text(
        json.dumps({"project_id": "p", "schema_version": 1, "rows": []})
    )
    sch = tmp_path / "b.kicad_sch"
    sch.write_text("(kicad_sch)")
    monkeypatch.setattr(s8, "_find_kicad_happy", lambda: tmp_path / "kh")

    def _fake_analyzer(script: Path, args: list[str], out_path: Path) -> dict:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({"findings": []}))
        return {"ok": True, "skipped": False, "exit_code": 0, "report_json": str(out_path)}

    monkeypatch.setattr(s8, "_run_analyzer", _fake_analyzer)

    def _fake_simulate(schematic_json, out_path, **_kw):
        if spice_report is None:
            return spice.SimRun(ok=True, skipped=True, reason="no simulator here")
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(spice_report))
        return spice._summarise(
            spice_report, simulator="ngspice", report_json=str(out_path)
        )

    monkeypatch.setattr(spice, "simulate", _fake_simulate)
    return s8.run(tmp_path, sch_path=sch, pcb_path=None, **kw)


def test_a_skipped_simulation_is_stated_in_the_review(tmp_path: Path, monkeypatch) -> None:
    """Omitting the section would let "nothing failed" and "nothing ran" look
    identical — the exact confusion this pipeline keeps designing out."""
    report = _stage8_with_spice(tmp_path, monkeypatch, None)
    assert report["simulation"]["skipped"] is True
    assert report["simulation"]["reason"] == "no simulator here"
    md = (tmp_path / ".pipeline" / "review.md").read_text()
    assert "## Simulation — not run" in md
    assert "unverified by simulation" in md


def test_simulation_findings_are_design_issues(tmp_path: Path, monkeypatch) -> None:
    """A filter that misses its cutoff is the design's problem, not the
    emitter's: the values came from the user's markdown."""
    finding = {
        "rule_id": "SP-FAIL",
        "severity": "error",
        "summary": "rc_filter R5/C3 — SPICE did not converge to expected values",
        "components": ["R5", "C3"],
    }
    report = _stage8_with_spice(
        tmp_path, monkeypatch, _report(_sim_result("fail"), findings=[finding])
    )
    assert report["summary"]["design"] == 1
    assert report["design_issues"][0]["source"] == "spice"
    assert report["design_issues"][0]["rule_id"] == "SP-FAIL"
    # Design issues never gate; only emitter defects and placeholders do.
    assert report["ok"] is True


def test_simulation_counts_reach_the_markdown(tmp_path: Path, monkeypatch) -> None:
    report = _stage8_with_spice(
        tmp_path, monkeypatch, _report(_sim_result("pass"), _sim_result("warn"))
    )
    assert report["simulation"]["counts"]["total"] == 2
    md = (tmp_path / ".pipeline" / "review.md").read_text()
    assert "## Simulation" in md
    assert "2 subcircuits simulated with ngspice" in md


def test_an_empty_simulation_says_so_in_the_review(tmp_path: Path, monkeypatch) -> None:
    report = _stage8_with_spice(tmp_path, monkeypatch, _report())
    assert report["simulation"]["nothing_to_simulate"] is True
    md = (tmp_path / ".pipeline" / "review.md").read_text()
    assert "## Simulation — nothing to simulate" in md
    assert "not** a pass" in md


def test_an_unmeasured_simulation_says_so_in_the_review(tmp_path: Path, monkeypatch) -> None:
    report = _stage8_with_spice(tmp_path, monkeypatch, _report(_sim_result("skip")))
    assert report["simulation"]["nothing_measured"] is True
    md = (tmp_path / ".pipeline" / "review.md").read_text()
    assert "## Simulation — no measurements came back" in md


def test_spice_false_leaves_simulation_unrun(tmp_path: Path, monkeypatch) -> None:
    report = _stage8_with_spice(
        tmp_path, monkeypatch, _report(_sim_result("pass")), spice=False
    )
    assert report["simulation"]["skipped"] is True
    assert report["simulation"]["reason"] == "not run"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_refuses_to_simulate_before_stage8(tmp_path: Path, capsys) -> None:
    from blpl.core import cli

    (tmp_path / ".pipeline").mkdir()
    rc = cli.main(["spice", "--project-dir", str(tmp_path)])
    assert rc == 2
    assert "run stage8 first" in capsys.readouterr().err


def test_cli_reports_a_missing_simulator_as_a_skip(tmp_path: Path, capsys, monkeypatch) -> None:
    from blpl.core import cli

    review = tmp_path / ".pipeline" / "review"
    review.mkdir(parents=True)
    (review / "schematic.json").write_text(json.dumps({"findings": []}))
    monkeypatch.setattr(
        spice,
        "find_simulator",
        lambda *_a, **_k: spice.SimulatorStatus(False, detail="install ngspice"),
    )
    rc = cli.main(["spice", "--project-dir", str(tmp_path)])
    assert rc == 2
    assert "install ngspice" in capsys.readouterr().err


def test_cli_prints_failures_and_the_report_path(tmp_path: Path, capsys, monkeypatch) -> None:
    from blpl.core import cli

    review = tmp_path / ".pipeline" / "review"
    review.mkdir(parents=True)
    (review / "schematic.json").write_text(json.dumps({"findings": []}))
    monkeypatch.setattr(
        spice, "find_simulator", lambda *_a, **_k: spice.SimulatorStatus(True, name="ngspice")
    )
    monkeypatch.setattr(
        spice,
        "simulate",
        lambda *a, **k: spice._summarise(
            _report(_sim_result("fail")), simulator="ngspice", report_json=str(review / "spice.json")
        ),
    )
    # A failing circuit is a finding about the design, so the command succeeds.
    assert cli.main(["spice", "--project-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "[fail] rc_filter R5/C3" in out
    assert "spice.json" in out


# ---------------------------------------------------------------------------
# The agent tool
# ---------------------------------------------------------------------------


def test_agent_tool_points_at_stage8_when_there_is_nothing_to_simulate(tmp_path) -> None:
    import asyncio

    from app.agent.registry import sim_tools
    from app.agent.toolspec import ToolContext
    from app.references import FilesystemSandbox

    spec = next(t for t in sim_tools() if t.name == "simulate_subcircuits")
    ctx = ToolContext(
        project_id="p",
        project_dir=tmp_path,
        sandbox=FilesystemSandbox(tmp_path),
    )
    with pytest.raises(FileNotFoundError, match="stage8"):
        asyncio.run(spec.handler(ctx, {}))


# ---------------------------------------------------------------------------
# End-to-end, when a simulator is actually installed
# ---------------------------------------------------------------------------

needs_ngspice = pytest.mark.skipif(
    not spice.find_simulator("ngspice").available,
    reason="ngspice not installed — the measurement path cannot be exercised",
)


def _rc_schematic(path: Path, *, farads: float, declared_hz: float = 1000.0) -> Path:
    """A single RC low-pass, in the shape analyze_schematic.py emits.

    1k with 159nF is 1/(2*pi*R*C) = 1000.97 Hz, so a declared 1 kHz is honest and
    the simulator has a right answer to find.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "analyzer_type": "schematic",
                "findings": [
                    {
                        "detector": "detect_rc_filters",
                        "rule_id": "RC-DET",
                        "severity": "info",
                        "summary": "RC low-pass R5/C3",
                        "components": ["R5", "C3"],
                        "resistor": {"ref": "R5", "ohms": 1000.0},
                        "capacitor": {"ref": "C3", "farads": farads},
                        "type": "low-pass",
                        "cutoff_hz": declared_hz,
                        "input_net": "AIN_RAW",
                        "output_net": "AIN_FILT",
                        "ground_net": "GND",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


@needs_ngspice
def test_a_correct_filter_measures_its_analytic_cutoff(tmp_path: Path) -> None:
    sch = _rc_schematic(tmp_path / "schematic.json", farads=1.59e-7)
    run = spice.simulate(sch, tmp_path / "spice.json", simulator="ngspice")

    assert run.ok is True and run.skipped is False
    assert run.simulator == "ngspice"
    assert run.counts["pass"] == 1
    assert run.nothing_measured is False
    # The measured cutoff has to be the real one, not merely present: 1k/159nF
    # is 1000.97 Hz analytically, and anything outside a percent means the
    # testbench is measuring something other than what we think it is.
    measured = run.results[0].simulated["cutoff_hz"]
    assert abs(measured - 1000.97) / 1000.97 < 0.01, measured


@needs_ngspice
def test_a_wrong_filter_is_caught(tmp_path: Path) -> None:
    """The test that matters. A checker that only ever agrees is indistinguishable
    from one that is not running — so give it a cap 10x too large for its stated
    cutoff and require it to say so."""
    sch = _rc_schematic(tmp_path / "schematic.json", farads=1.59e-6)
    run = spice.simulate(sch, tmp_path / "spice.json", simulator="ngspice")

    assert run.counts["fail"] == 1
    assert run.results[0].status == "fail"
    # A decade of capacitance is a decade of cutoff: ~100 Hz against a declared
    # 1 kHz. kicad-happy reports fc_error_pct as magnitude-off rather than
    # signed direction, so assert on size — ~90%.
    assert abs(run.results[0].delta["fc_error_pct"]) > 80
    assert run.results[0].simulated["cutoff_hz"] < 200


@needs_ngspice
def test_a_failing_simulation_becomes_a_design_issue(tmp_path: Path) -> None:
    """End of the chain: the finding has to survive into Stage 8 on the *design*
    side. The component values came from the user's markdown, so a filter that
    misses its cutoff is never an emitter defect."""
    review = tmp_path / "review"
    _rc_schematic(review / "schematic.json", farads=1.59e-6)
    result = s8._run_spice(review / "schematic.json", review, pcb_json=None)

    assert result["ok"] is True and result["skipped"] is False
    assert result["counts"]["fail"] == 1
    report = json.loads((review / "spice.json").read_text(encoding="utf-8"))
    finding = report["findings"][0]
    assert finding["rule_id"] == "SP-FAIL"
    assert s8._classify(finding, {}) == ("design", None)


@needs_ngspice
def test_an_unroutable_board_still_simulates_on_ideal_nets(tmp_path: Path) -> None:
    """A PCB with no traces yields no parasitics, and that has to be a stated
    basis for the result rather than a silent one."""
    review = tmp_path / "review"
    sch = _rc_schematic(review / "schematic.json", farads=1.59e-7)
    pcb = review / "pcb.json"
    pcb.write_text(json.dumps({"net_lengths": [{"net": "AIN_FILT"}]}), encoding="utf-8")

    run = spice.simulate(sch, review / "spice.json", pcb_json=pcb, simulator="ngspice")
    assert run.counts["pass"] == 1
    assert run.parasitics is False
    assert "no routed traces" in run.parasitics_note
    assert "ideal nets" in run.headline()
