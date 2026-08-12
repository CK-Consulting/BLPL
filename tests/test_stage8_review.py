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
    pcb = {"footprints": [{} for _ in range(6)]}
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
        provenance, owner = s8._classify(_finding(rule), {})
        assert provenance == "emitter", rule
        assert owner


def test_unrouted_nets_are_expected_not_failures() -> None:
    # BLPL has no autorouter; reporting these as errors would be pure noise.
    provenance, _ = s8._classify(_finding("RT-001"), {})
    assert provenance == "expected"


def test_sourcing_blocker_is_a_design_issue_when_the_bom_never_had_mpns() -> None:
    # SS-001 only indicts the emitter if there were MPNs available to lose.
    provenance, _ = s8._classify(_finding("SS-001"), {})
    assert provenance == "design"

    provenance, owner = s8._classify(_finding("SS-001"), {"mpn_dropped": True})
    assert provenance == "emitter"
    assert owner == "emitter/sch.py"


def test_genuine_electrical_finding_is_a_design_issue() -> None:
    provenance, owner = s8._classify(_finding("DC-002"), {})
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

    def _fake_run(script: Path, args: list[str], out_path: Path) -> dict:
        # One real design problem, one known limitation. No emitter defects:
        # the single BOM row was emitted as one symbol, with its MPN intact.
        payload = {
            "statistics": {"total_components": 1},
            "bom_lock": {"components_with_mpn": 1},
            "findings": [_finding("DC-002"), _finding("RT-001")],
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload))
        return {"ok": True, "skipped": False, "exit_code": 0, "report_json": str(out_path)}

    monkeypatch.setattr(s8, "_run_analyzer", _fake_run)

    # spice=False keeps this hermetic: simulation shells out to a real binary
    # that may or may not be installed, and this test is about classification.
    report = s8.run(tmp_path, sch_path=sch, pcb_path=None, spice=False)
    assert report["summary"] == {"emitter": 0, "design": 1, "expected": 1, "placeholders": 0}
    assert report["ok"] is True
    assert "DC-002" in (pipeline / "review.md").read_text()


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
        {"rule_id": "RS-001", "nets": ["VCC_3V3"]}, {}, {"VCC_3V3", "GND"}
    )
    assert flagged[0] == "expected"

    unflagged = s8._classify(
        {"rule_id": "RS-001", "nets": ["VDD_UNSOURCED"]}, {}, {"VCC_3V3", "GND"}
    )
    assert unflagged[0] == "emitter", "a rail with no PWR_FLAG must still be reported"

    # And with no evidence file at all, nothing is excused.
    assert s8._classify({"rule_id": "RS-001", "nets": ["VCC_3V3"]}, {}, set())[0] == "emitter"
