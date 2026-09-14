"""Unit tests for blpl.core.stage8_review.

The kicad-happy analyzers are stubbed here so these tests don't depend on the
submodule being checked out. What we're actually testing is the part BLPL owns:
provenance classification — deciding whether a finding on a generated board
indicts the emitter, the design, or neither.
"""

from __future__ import annotations

import json
from pathlib import Path

from blpl.core import stage8_review as s8


def _bom(rows: list[dict]) -> dict:
    return {"project_id": "proj", "schema_version": 1, "rows": rows}


def _finding(rule_id: str, severity: str = "error", summary: str = "x") -> dict:
    return {"rule_id": rule_id, "severity": severity, "summary": summary}


def _ctx(evidence: dict | None = None, pwr_flag_nets: set[str] | None = None, **kw) -> s8._Context:
    """A classification context with nothing having run unless a test says so."""
    return s8._Context(
        pwr_flag_nets=pwr_flag_nets or set(),
        autoroute=kw.get("autoroute"),
        test_points=kw.get("test_points"),
        lifecycle_reason=kw.get("lifecycle_reason"),
        emitter_evidence=evidence or {},
    )


def test_skips_cleanly_when_kicad_happy_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(s8, "_find_kicad_happy", lambda: None)
    report = s8.run(tmp_path)
    assert report["skipped"] is True
    # A missing reviewer must not fail the pipeline — same contract as stage7.
    assert report["ok"] is True
    assert (tmp_path / ".pipeline" / "review_report.json").exists()


def test_mpn_dropped_detected_when_bom_has_mpns_but_schematic_does_not() -> None:
    bom = _bom([{"local_id": "U1", "mpn": "TPS65086RSMR", "package": "QFN-48"}])
    sch = {"statistics": {"total_components": 1}, "bom_lock": {"components_with_mpn": 0}}
    checks = s8._emitter_crosschecks(bom, sch, {})
    assert [c["check"] for c in checks] == ["mpn_dropped"]
    assert checks[0]["owner"] == "emitter/sch.py"


def test_no_mpn_defect_when_emitter_preserved_them() -> None:
    bom = _bom([{"local_id": "U1", "mpn": "TPS65086RSMR", "package": "QFN-48"}])
    sch = {"statistics": {"total_components": 1}, "bom_lock": {"components_with_mpn": 1}}
    assert s8._emitter_crosschecks(bom, sch, {}) == []


def test_component_leakage_between_bom_and_emitted_files() -> None:
    bom = _bom([{"local_id": f"U{i}", "mpn": "M", "package": "P"} for i in range(15)])
    sch = {"statistics": {"total_components": 12}, "bom_lock": {"components_with_mpn": 15}}
    # Analyzer output without reference lists: the count comparison still fires.
    pcb = {"statistics": {"footprint_count": 6}}
    checks = {c["check"] for c in s8._emitter_crosschecks(bom, sch, pcb)}
    assert checks == {"symbol_leakage", "footprint_leakage"}


def test_total_emitter_loss_still_gates_when_pcb_present_but_empty() -> None:
    # The worst leakage: every symbol and footprint vanished. A count-based guard
    # (`sch_count and ...`) would skip both checks on 0 and pass an empty board.
    bom = _bom([{"local_id": f"U{i}", "mpn": "M", "package": "P"} for i in range(5)])
    sch = {"statistics": {"total_components": 0}, "bom_lock": {"components_with_mpn": 0}}
    pcb = {"statistics": {"footprint_count": 0}, "footprints": []}
    checks = {c["check"] for c in s8._emitter_crosschecks(bom, sch, pcb)}
    assert checks == {"symbol_leakage", "footprint_leakage"}


def test_no_footprint_leakage_when_no_pcb_was_emitted() -> None:
    # An absent PCB (no .kicad_pcb supplied → empty analysis dict) must NOT be
    # flagged as "0 footprints placed" — there was no PCB to leak from.
    bom = _bom([{"local_id": "U1", "mpn": "M", "package": "P"}])
    sch = {"statistics": {"total_components": 1}, "bom_lock": {"components_with_mpn": 1}}
    checks = {c["check"] for c in s8._emitter_crosschecks(bom, sch, {})}
    assert "footprint_leakage" not in checks


def test_schematic_pcb_disagreement_is_always_an_emitter_defect() -> None:
    # Both files come from one hdm.yaml, so they cannot legitimately disagree.
    for rule in ("XV-001", "XV-002"):
        provenance, owner = s8._classify(_finding(rule), _ctx())
        assert provenance == "emitter", rule
        assert owner


def test_unrouted_nets_are_expected_only_while_nothing_has_routed() -> None:
    """RT-001 is excused with the reason routing did not happen — and stops
    being excused the moment a router has had a go, because then an open net
    is a statement about the board."""
    ctx = _ctx(autoroute={"attempted": False, "ok": False, "reason": "the Freerouting jar is missing"})
    provenance, _ = s8._classify(_finding("RT-001"), ctx)
    assert provenance == "expected"
    assert "Freerouting jar is missing" in ctx.reasons["RT-001"]

    routed = _ctx(autoroute={"attempted": True, "ok": True, "unrouted": 3})
    assert s8._classify(_finding("RT-001"), routed)[0] == "design"

    # A router that ran and failed is not a router that ran.
    failed = _ctx(autoroute={"attempted": True, "ok": False, "reason": "Specctra export failed"})
    assert s8._classify(_finding("RT-001"), failed)[0] == "expected"


def test_test_point_coverage_is_the_designers_call_with_the_knob_named() -> None:
    """TE-001 used to be a permanent 'BLPL does not synthesise test points'.
    It does now, under a policy — so the finding is a design issue and its
    recommendation names the policy rather than kicad-happy's generic advice."""
    ctx = _ctx(test_points={"policy": "power", "count": 12})
    assert s8._classify(_finding("TE-001", "warning"), ctx)[0] == "design"
    rec = s8._test_point_recommendation(
        {"rule_id": "TE-001", "recommendation": "Add test points."}, {"policy": "power", "count": 12}
    )
    assert "test_points" in rec and "policy: all" in rec and "12" in rec
    # Under `all` the leftover is nets with nothing to probe.
    rec_all = s8._test_point_recommendation({"rule_id": "TE-001"}, {"policy": "all", "count": 400})
    assert "Nothing to do" in rec_all
    # Other rules keep whatever the analyzer said.
    assert s8._test_point_recommendation({"rule_id": "DC-002", "recommendation": "r"}, {}) == "r"


def test_lifecycle_skip_carries_its_reason() -> None:
    ctx = _ctx(lifecycle_reason="no distributor credentials configured — set MOUSER_SEARCH_API_KEY")
    assert s8._classify(_finding("LC-007", "info"), ctx)[0] == "expected"
    assert "MOUSER_SEARCH_API_KEY" in ctx.reasons["LC-007"]


def test_sourcing_blocker_is_a_design_issue_when_the_bom_never_had_mpns() -> None:
    # SS-001 only indicts the emitter if there were MPNs available to lose.
    provenance, _ = s8._classify(_finding("SS-001"), _ctx())
    assert provenance == "design"

    provenance, owner = s8._classify(_finding("SS-001"), _ctx({"mpn_dropped": True}))
    assert provenance == "emitter"
    assert owner == "emitter/sch.py"


def test_genuine_electrical_finding_is_a_design_issue() -> None:
    provenance, owner = s8._classify(_finding("DC-002"), _ctx())
    assert provenance == "design"
    assert owner is None


def test_emitter_defects_gate_but_design_issues_do_not(tmp_path: Path, monkeypatch) -> None:
    """`ok` reflects pipeline correctness, not design quality.

    A board with real electrical problems still means the *emitter* did its job.
    Those are the user's to triage; only emitter defects gate the pipeline.
    """
    pipeline = tmp_path / ".pipeline"
    pipeline.mkdir(parents=True)
    (pipeline / "bom.json").write_text(
        json.dumps(_bom([{"local_id": "U1", "mpn": "M", "package": "P"}]))
    )
    sch = tmp_path / "b.kicad_sch"
    sch.write_text("(kicad_sch)")

    fake = tmp_path / "kh"
    monkeypatch.setattr(s8, "_find_kicad_happy", lambda: fake)

    def _fake_run(script: Path, args: list[str], out_path: Path, env=None) -> dict:
        # One real design problem, one known limitation. No emitter defects:
        # the single BOM row was emitted as one symbol, with its MPN intact.
        payload = {
            "statistics": {"total_components": 1},
            "bom_lock": {"components_with_mpn": 1},
            "components": [{"reference": "U1"}],
            "findings": [_finding("DC-002"), _finding("RT-001")],
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload))
        return {"ok": True, "skipped": False, "exit_code": 0, "report_json": str(out_path)}

    monkeypatch.setattr(s8, "_run_analyzer", _fake_run)

    # spice=False keeps this hermetic: simulation shells out to a real binary
    # that may or may not be installed, and this test is about classification.
    report = s8.run(tmp_path, sch_path=sch, pcb_path=None, spice=False, lifecycle=False)
    assert report["summary"] == {"emitter": 0, "design": 1, "expected": 1, "placeholders": 0}
    assert report["ok"] is True
    md = (pipeline / "review.md").read_text()
    assert "DC-002" in md
    # The excuse for RT-001 is written next to it, not implied.
    assert "no autoroute step ran" in md
    assert report["lifecycle"] == {"ran": False, "reason": "skipped on request (--no-lifecycle)"}


def test_a_board_qualifies_every_artifact_stage8_touches(tmp_path: Path, monkeypatch) -> None:
    """On a multi-board project the BOM is bom.sensor.json and the review must be
    review_report.sensor.json — reading the unqualified names reviews whichever
    board last wrote them and calls it this one."""
    pipeline = tmp_path / ".pipeline"
    pipeline.mkdir(parents=True)
    (pipeline / "bom.sensor.json").write_text(
        json.dumps(_bom([{"local_id": "U7", "mpn": "M", "package": "P"}]))
    )
    sch = tmp_path / "b.kicad_sch"
    sch.write_text("(kicad_sch)")
    monkeypatch.setattr(s8, "_find_kicad_happy", lambda: tmp_path)

    def _fake_run(script: Path, args: list[str], out_path: Path, env=None) -> dict:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({"statistics": {"total_components": 0},
                                        "components": [], "findings": []}))
        return {"ok": True, "skipped": False, "exit_code": 0, "report_json": str(out_path)}

    monkeypatch.setattr(s8, "_run_analyzer", _fake_run)
    report = s8.run(tmp_path, sch_path=sch, spice=False, board="sensor", lifecycle=False)
    assert report["board"] == "sensor"
    assert (pipeline / "review_report.sensor.json").is_file()
    assert (pipeline / "review.sensor.md").is_file()
    assert (pipeline / "review.sensor" / "schematic.json").is_file()
    # U7 came from bom.sensor.json and is missing from the (empty) schematic.
    assert [c["check"] for c in report["emitter_defects"]] == ["symbol_leakage"]
    assert report["emitter_defects"][0]["components"] == ["U7"]


def test_leakage_is_reported_by_name_and_skips_what_was_never_placeable() -> None:
    """The coin cell that sits in a retainer has a BOM row and no footprint on
    purpose; Stage 5's test points have footprints and no BOM row on purpose.
    Neither is a leak, and the row that IS missing is named."""
    bom = _bom([
        {"local_id": "U1", "mpn": "M", "package": "P"},
        {"local_id": "BAT1", "mpn": "ML1220", "package": "not_placed"},
        {"local_id": "J9", "mpn": "M", "package": "P"},
    ])
    hdm = {"components": {"U1": {}, "J9": {}, "TP1": {"synthesized": "test_point"}}}
    sch = {"statistics": {"total_components": 1}, "bom_lock": {"components_with_mpn": 1},
           "components": [{"reference": "U1"}, {"reference": "#FLG01"}]}
    pcb = {"footprints": [{"reference": "U1"}, {"reference": "TP1"}, {"reference": "REF**"}]}
    checks = {c["check"]: c for c in s8._emitter_crosschecks(bom, sch, pcb, hdm)}
    assert set(checks) == {"symbol_leakage", "footprint_leakage", "footprint_unaccounted"}
    assert checks["symbol_leakage"]["components"] == ["J9"]
    assert checks["footprint_leakage"]["components"] == ["J9"]
    assert checks["footprint_unaccounted"]["components"] == ["REF**"]
    assert "BAT1" not in json.dumps(checks)


def test_no_leak_when_everything_placeable_landed() -> None:
    bom = _bom([{"local_id": "U1", "mpn": "M", "package": "P"},
                {"local_id": "BAT1", "mpn": "ML1220", "package": "Not placed"}])
    sch = {"statistics": {"total_components": 1}, "bom_lock": {"components_with_mpn": 1},
           "components": [{"reference": "U1"}]}
    pcb = {"footprints": [{"reference": "U1"}]}
    assert s8._emitter_crosschecks(bom, sch, pcb, {}) == []


def test_stage1_component_loss_is_detected() -> None:
    """Stage 0 is deterministic, so a shorter BOM means Stage 1 lost components."""
    da = {"components": [{"local_id": f"U{i}"} for i in range(46)]}
    bom = _bom([{"local_id": f"U{i}", "mpn": "M", "package": "P"} for i in range(13)])
    checks = s8._stage1_crosscheck(da, bom)
    assert [c["check"] for c in checks] == ["stage1_component_loss"]
    assert "33 were dropped" in checks[0]["summary"]


def test_no_loss_reported_when_stage1_kept_everything() -> None:
    da = {"components": [{"local_id": "U1"}, {"local_id": "U2"}]}
    bom = _bom([{"local_id": "U1", "mpn": "M", "package": "P"},
                {"local_id": "U2", "mpn": "M", "package": "P"}])
    assert s8._stage1_crosscheck(da, bom) == []


def test_a_placeholder_part_blocks_fabrication() -> None:
    """The last gate before someone treats generated output as fabricable.

    Stage 5 substitutes generic stand-ins for parts it can't find so the board
    still opens and renders — which is precisely what makes them dangerous. A
    board with a 2.54mm header standing in for a QFN is not "ok" at any severity
    below error.
    """
    hdm = {
        "components": {
            "U_IMU": {
                "needs_manual_footprint": True,
                "requested_footprint": "Sensor_Motion:InvenSense_QFN-14",
                "needs_manual_symbol": True,
                "requested_symbol": "Sensor_Motion:ICM-42670-P",
            },
            "R1": {},
        }
    }
    found = s8._placeholder_check(hdm)
    kinds = {f["check"] for f in found}
    assert kinds == {"placeholder_footprint", "placeholder_symbol"}
    assert all(f["severity"] == "error" for f in found)
    assert all(f["refdes"] == "U_IMU" for f in found)
    assert "wrong copper" in next(f for f in found if f["check"] == "placeholder_footprint")["summary"]


def test_a_board_with_no_placeholders_reports_none() -> None:
    assert s8._placeholder_check({"components": {"R1": {}, "C1": {}}}) == []


def test_rs001_is_excused_only_on_a_rail_we_actually_flagged() -> None:
    """kicad-happy cannot see a PWR_FLAG, so it reports every flagged rail as
    unsourced. Excusing RS-001 outright would hide a genuinely unsourced rail —
    the excuse has to be evidence-based, net by net."""
    flagged = s8._classify(
        {"rule_id": "RS-001", "nets": ["VCC_3V3"]}, _ctx(pwr_flag_nets={"VCC_3V3", "GND"})
    )
    assert flagged[0] == "expected"

    unflagged = s8._classify(
        {"rule_id": "RS-001", "nets": ["VDD_UNSOURCED"]}, _ctx(pwr_flag_nets={"VCC_3V3", "GND"})
    )
    assert unflagged[0] == "emitter", "a rail with no PWR_FLAG must still be reported"

    # And with no evidence file at all, nothing is excused.
    assert s8._classify({"rule_id": "RS-001", "nets": ["VCC_3V3"]}, _ctx())[0] == "emitter"


def test_an_autoroute_record_for_another_compile_does_not_count(tmp_path: Path, monkeypatch) -> None:
    pipeline = tmp_path / ".pipeline"
    pipeline.mkdir(parents=True)
    (pipeline / "bom.json").write_text(json.dumps(_bom([{"local_id": "U1", "mpn": "M", "package": "P"}])))
    (pipeline / "autoroute_report.json").write_text(json.dumps(
        {"attempted": True, "ok": True, "pcb": str(pipeline / "old_2026-01-01.kicad_pcb"), "unrouted": 0}))
    pcb = pipeline / "new_2026-02-02.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    monkeypatch.setattr(s8, "_find_kicad_happy", lambda: tmp_path)

    def _fake_run(script: Path, args: list[str], out_path: Path, env=None) -> dict:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({"footprints": [{"reference": "U1"}],
                                        "findings": [_finding("RT-001")]}))
        return {"ok": True, "skipped": False, "exit_code": 0, "report_json": str(out_path)}

    monkeypatch.setattr(s8, "_run_analyzer", _fake_run)
    report = s8.run(tmp_path, pcb_path=pcb, spice=False, lifecycle=False)
    assert report["autoroute"]["attempted"] is False
    assert "old_2026-01-01" in report["autoroute"]["reason"]
    assert report["expected"] == [{"rule_id": "RT-001", "count": 1}]
