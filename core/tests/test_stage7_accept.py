"""Stage 7 applies project.yaml review.accept to ERC and DRC violations.

ERC and DRC fail on any violation, warnings included, so a finding that is true
and intended failed Stage 7 on every run. The case that forced this was
ear-tag-bm20: two pin_to_pin warnings, each a PWR_FLAG the emitter adds meeting
a bidirectional pin tied to that rail on purpose (an IMU's SDO strapped to GND,
a GPIO powering a GNSS backup pin). The reports below are shaped like that one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blpl.core import stage7_validate as s7


def _pin_to_pin(ref: str, pin: str, flag: str) -> dict:
    return {
        "description": "Pins of type Bidirectional and Power output are connected",
        "items": [
            {"description": f"Symbol {ref} Pin {pin} [SDO, Bidirectional, Line]"},
            {"description": f"Symbol {flag} Pin 1 [Power output, Line]"},
        ],
        "severity": "warning",
        "type": "pin_to_pin",
    }


EAR_TAG_ERC = {"sheets": [{"path": "/", "violations": [
    _pin_to_pin("U1", "E7", "#FLG02"),
    _pin_to_pin("U6", "1", "#FLG01"),
]}]}

BOTH = [
    {"rule": "erc:pin_to_pin", "parts": ["U6"], "reason": "BMI323 SDO strapped to GND for its address."},
    {"rule": "erc:pin_to_pin", "parts": ["U1"], "reason": "nRF GPIO powers the GNSS V_BCKP pin."},
]


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".pipeline").mkdir(parents=True)
    (proj / "x.kicad_sch").write_text("(kicad_sch)")
    (proj / "x.kicad_pcb").write_text("(kicad_pcb)")
    return proj


@pytest.fixture()
def fake_cli(monkeypatch):
    """kicad-cli stood in for: writes the given report and exits as it would."""
    reports: dict[str, dict] = {}

    def _runner(kind: str):
        def run(_path: Path, output_json: Path) -> dict:
            data = reports[kind]
            output_json.write_text(json.dumps(data))
            n = (s7.erc_violation_count(data) if kind == "erc" else
                 len(data.get("violations") or []) + len(data.get("unconnected_items") or []))
            return {"ok": n == 0, "skipped": False, "exit_code": 5 if n else 0,
                    "violation_count": len(data.get("violations") or []) if kind == "drc"
                    else n, "report_json": str(output_json)}
        return run

    monkeypatch.setattr(s7, "_run_erc", _runner("erc"))
    monkeypatch.setattr(s7, "_run_drc", _runner("drc"))
    reports["drc"] = {"violations": [], "unconnected_items": [], "schematic_parity": []}
    return reports


def _run(proj: Path, accept: list[dict] | None) -> dict:
    cfg = {"review": {"accept": accept}} if accept is not None else None
    return s7.run(proj, sch_path=proj / "x.kicad_sch", pcb_path=proj / "x.kicad_pcb",
                  project_config=cfg)


def test_without_accept_entries_the_warnings_still_fail(tmp_path, fake_cli):
    fake_cli["erc"] = EAR_TAG_ERC
    proj = _project(tmp_path)
    for cfg in (None, []):
        report = _run(proj, cfg)
        assert report["erc"]["ok"] is False
        assert "accepted" not in report["erc"]
        assert report["ok"] is False


def test_accepted_erc_warnings_no_longer_fail_and_stay_on_record(tmp_path, fake_cli):
    fake_cli["erc"] = EAR_TAG_ERC
    proj = _project(tmp_path)
    report = _run(proj, BOTH)

    erc = report["erc"]
    assert erc["ok"] is True
    assert erc["violation_count"] == 2, "what kicad-cli found is still what is reported"
    assert erc["accepted_count"] == 2
    reasons = {tuple(a["components"]): a["accepted_reason"] for a in erc["accepted"]}
    assert reasons == {("U1",): BOTH[1]["reason"], ("U6",): BOTH[0]["reason"]}
    assert all(a["rule_id"] == "erc:pin_to_pin" for a in erc["accepted"])
    # The violation itself is kept whole, items and all.
    assert erc["accepted"][0]["items"][1]["description"].startswith("Symbol #FLG")

    written = json.loads((proj / ".pipeline" / "validation_report.json").read_text())
    assert written["erc"]["accepted_count"] == 2
    assert written["ok"] is True


def test_an_unmatched_violation_still_fails(tmp_path, fake_cli):
    fake_cli["erc"] = EAR_TAG_ERC
    report = _run(_project(tmp_path), BOTH[:1])
    assert report["erc"]["ok"] is False
    assert report["erc"]["accepted_count"] == 1
    assert report["erc"]["accepted"][0]["components"] == ["U6"]


def test_generated_flag_refs_are_never_required(tmp_path, fake_cli):
    # #FLG numbering moves between runs: listing U6 alone has to be enough,
    # whichever flag it met this time.
    fake_cli["erc"] = {"sheets": [{"violations": [_pin_to_pin("U6", "1", "#FLG07")]}]}
    report = _run(_project(tmp_path), BOTH[:1])
    assert report["erc"]["ok"] is True


def test_parts_must_cover_every_real_part_the_violation_names(tmp_path, fake_cli):
    two_parts = {
        "description": "Pins of type Output and Output are connected",
        "items": [{"description": "Symbol U2 Pin 3 [OUT, Output, Line]"},
                  {"description": "Symbol U3 Pin 4 [OUT, Output, Line]"}],
        "severity": "error", "type": "pin_to_pin",
    }
    fake_cli["erc"] = {"sheets": [{"violations": [two_parts]}]}
    proj = _project(tmp_path)

    only_u2 = [{"rule": "erc:pin_to_pin", "parts": ["U2"], "reason": "Not enough on its own."}]
    assert _run(proj, only_u2)["erc"]["ok"] is False

    superset = [{"rule": "erc:*", "parts": ["U2", "U3", "U9"], "reason": "Open-drain in practice."}]
    assert _run(proj, superset)["erc"]["ok"] is True


def test_a_wrong_rule_or_part_covers_nothing(tmp_path, fake_cli):
    fake_cli["erc"] = EAR_TAG_ERC
    proj = _project(tmp_path)
    assert "accepted" not in _run(proj, [{"rule": "erc:pin_not_connected", "reason": "Different rule."}])["erc"]
    assert "accepted" not in _run(proj, [{"rule": "erc:pin_to_pin", "parts": ["U9"], "reason": "Other part."}])["erc"]


def test_entries_for_other_stages_never_waive_erc(tmp_path, fake_cli):
    # A bare "*" written for Stage 8 must not quietly pass the schematic.
    fake_cli["erc"] = EAR_TAG_ERC
    report = _run(_project(tmp_path), [{"rule": "*", "reason": "Stage 8 noise for now."}])
    assert report["erc"]["ok"] is False
    assert "accepted" not in report["erc"]


def test_drc_violations_match_on_footprints_and_nets(tmp_path, fake_cli):
    fake_cli["erc"] = {"sheets": []}
    fake_cli["drc"] = {"violations": [{
        "description": "Clearance violation",
        "items": [{"description": "Pad 1 [GND] of C3 on F.Cu"},
                  {"description": "Track [VDD_3V3] on F.Cu, length 1.0 mm"}],
        "severity": "error", "type": "clearance",
    }], "unconnected_items": [], "schematic_parity": []}
    proj = _project(tmp_path)

    report = _run(proj, [{"rule": "drc:clearance", "parts": ["C3"], "nets": ["GND", "VDD_3V3"],
                          "reason": "Fab confirmed the tighter clearance."}])
    assert report["drc"]["ok"] is True
    assert report["drc"]["accepted"][0]["nets"] == ["GND", "VDD_3V3"]

    report = _run(proj, [{"rule": "drc:clearance", "nets": ["GND"], "reason": "Only one of the two nets."}])
    assert report["drc"]["ok"] is False


def test_unconnected_items_are_never_waived(tmp_path, fake_cli):
    fake_cli["erc"] = {"sheets": []}
    fake_cli["drc"] = {"violations": [{
        "description": "Track has unconnected end",
        "items": [{"description": "Track [VDD_3V3] on B.Cu, length 1.4 mm"}],
        "severity": "warning", "type": "track_dangling",
    }], "unconnected_items": [{
        "description": "Missing connection between items",
        "items": [{"description": "Pad A2 [VDD_3V3] of U5 on F.Cu"}],
        "severity": "error", "type": "unconnected_items",
    }], "schematic_parity": []}
    report = _run(_project(tmp_path), [{"rule": "drc:*", "reason": "Trying to waive everything."}])

    drc = report["drc"]
    assert drc["accepted_count"] == 1, "the dangling track is waivable"
    assert all(a["type"] != "unconnected_items" for a in drc["accepted"])
    assert drc["ok"] is False, "an open connection keeps failing the check"


def test_a_failed_run_is_not_turned_into_a_pass(tmp_path, monkeypatch):
    # A report on disk beside an exit code that is not "violations found" is
    # not a pass, however many of its findings are accepted.
    proj = _project(tmp_path)
    out = proj / ".pipeline" / "erc_report.json"

    def run_erc(_p, output_json):
        output_json.write_text(json.dumps(EAR_TAG_ERC))
        return {"ok": False, "skipped": False, "exit_code": 3, "violation_count": 2}

    monkeypatch.setattr(s7, "_run_erc", run_erc)
    report = s7.run(proj, sch_path=proj / "x.kicad_sch", project_config={"review": {"accept": BOTH}})
    assert out.exists()
    assert report["erc"]["ok"] is False
