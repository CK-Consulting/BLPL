"""Stage 7 counts every ERC violation, wherever the report puts it.

KiCad 10's ERC report lists violations per sheet and has no top-level list, so
the count read 0 while kicad-cli printed "Found 6 violations": a number that
looked clean on a schematic that was not.
"""

from __future__ import annotations

import json
import subprocess

from blpl.core import stage7_validate

KICAD10 = {"sheets": [
    {"path": "/", "violations": [{"type": "pin_not_connected"}, {"type": "isolated_pin_label"}]},
    {"path": "/power", "violations": [{"type": "isolated_pin_label"}]},
]}


def test_violations_are_counted_on_every_sheet():
    assert stage7_validate.erc_violation_count(KICAD10) == 3
    assert stage7_validate.erc_violation_count({"violations": [{}, {}]}) == 2
    assert stage7_validate.erc_violation_count({}) == 0


def test_run_erc_reports_a_kicad10_reports_count(tmp_path, monkeypatch):
    out = tmp_path / "erc_report.json"
    monkeypatch.setattr(stage7_validate, "_find_kicad_cli", lambda: "/usr/bin/true")
    monkeypatch.setattr(stage7_validate, "_kicad_cli_major", lambda _cli: 10)

    def fake_cli(*_a, **_k):
        out.write_text(json.dumps(KICAD10))
        return subprocess.CompletedProcess(args=[], returncode=5, stdout="Found 3 violations\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_cli)
    sch = tmp_path / "b.kicad_sch"
    sch.write_text("(kicad_sch)")
    result = stage7_validate._run_erc(sch, out)
    assert result["ok"] is False and result["violation_count"] == 3
