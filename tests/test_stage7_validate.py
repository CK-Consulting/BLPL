"""Unit tests for pipeline.stage7_validate.

All external-tool paths are stubbed so these tests don't depend on kicad-cli or
klc-check being installed. A separate (opt-in) smoke test exercises the real tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blpl.core import stage7_validate as s7


def _write_coverage(path: Path, hit: int, miss: int) -> None:
    data = {
        "project_id": "proj",
        "schema_version": 1,
        "generated_at": "2026-04-20T00:00:00+00:00",
        "library_roots": {"symbols": "/x", "footprints": "/y"},
        "rows": [],
        "summary": {"total": hit + miss, "hit": hit, "needs_variant": 0, "miss": miss},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_overall_ok_when_coverage_clean_and_no_pcb_or_symbols(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / ".pipeline").mkdir(parents=True)
    _write_coverage(proj / ".pipeline" / "coverage_report.json", hit=5, miss=0)
    report = s7.run(proj)
    assert report["ok"] is True
    assert report["coverage"]["miss"] == 0
    assert report["drc"]["skipped"] is True
    assert report["klc_symbols"]["ok"] is True  # no symbols to check


def test_coverage_miss_fails_overall(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / ".pipeline").mkdir(parents=True)
    _write_coverage(proj / ".pipeline" / "coverage_report.json", hit=3, miss=2)
    report = s7.run(proj)
    assert report["ok"] is False
    assert report["coverage"]["miss"] == 2


def test_drc_is_invoked_when_pcb_supplied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "proj"
    (proj / ".pipeline").mkdir(parents=True)
    _write_coverage(proj / ".pipeline" / "coverage_report.json", hit=1, miss=0)
    pcb = proj / ".pipeline" / "test.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    called = {}

    def fake_run_drc(pcb_path: Path, output_json: Path) -> dict:
        called["pcb"] = pcb_path
        called["out"] = output_json
        return {"ok": True, "skipped": False, "kicad_cli_major": 10, "exit_code": 0, "violation_count": 0}

    monkeypatch.setattr(s7, "_run_drc", fake_run_drc)
    report = s7.run(proj, pcb_path=pcb)
    assert called["pcb"] == pcb
    assert called["out"].name == "drc_report.json"
    assert report["drc"]["kicad_cli_major"] == 10


def test_klc_skipped_gracefully_when_script_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "proj"
    (proj / ".pipeline").mkdir(parents=True)
    _write_coverage(proj / ".pipeline" / "coverage_report.json", hit=1, miss=0)
    # Point the script location at a non-existent path.
    monkeypatch.setattr(s7, "_CHECK_SYMBOL_PY", tmp_path / "does_not_exist.py")
    # Put a fake .kicad_sym in the generated symbols dir.
    gen = proj / "generated" / "symbols"
    gen.mkdir(parents=True)
    (gen / "X.kicad_sym").write_text("(kicad_symbol_lib)")
    report = s7.run(proj)
    assert report["klc_symbols"]["skipped"] is True
    assert "not found" in report["klc_symbols"]["reason"]


def test_validation_report_json_is_written(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / ".pipeline").mkdir(parents=True)
    _write_coverage(proj / ".pipeline" / "coverage_report.json", hit=1, miss=0)
    s7.run(proj)
    report_path = proj / ".pipeline" / "validation_report.json"
    assert report_path.exists()
    loaded = json.loads(report_path.read_text())
    assert "ok" in loaded
    assert "klc_symbols" in loaded
    assert "drc" in loaded
    assert "coverage" in loaded
