"""Stage 7 must not report a previous run's findings as this run's.

kicad-cli writes no report when it cannot load the design at all. Stage 7
decided what happened by asking whether the report file existed, so a stale
file from an earlier run was read as the current result — and a schematic
kicad-cli refused to open came back looking merely *unchanged*. That is the
worst way for an emitter bug to present: the number you are watching does not
move, so nothing tells you to look.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blpl.core import stage7_validate


@pytest.fixture()
def stale(tmp_path: Path) -> Path:
    report = tmp_path / "erc_report.json"
    report.write_text(json.dumps({"violations": [{"description": "from the last run"}] * 53}))
    return report


def _cli_that_writes_nothing(monkeypatch, returncode: int = 3):
    """Stand in for kicad-cli failing to load the design."""
    import subprocess

    monkeypatch.setattr(stage7_validate, "_find_kicad_cli", lambda: "/usr/bin/true")
    monkeypatch.setattr(stage7_validate, "_kicad_cli_major", lambda _cli: 9)

    def _run(*_a, **_k):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout="Failed to load schematic\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _run)


def test_erc_does_not_inherit_a_previous_reports_violations(tmp_path, stale, monkeypatch):
    _cli_that_writes_nothing(monkeypatch)
    result = stage7_validate._run_erc(tmp_path / "x.kicad_sch", stale)

    assert not stale.exists(), "the old report must be cleared before the run"
    assert result["report_json"] is None
    assert "violation_count" not in result, "no report means no count, not last time's count"
    assert result["ok"] is False


def test_drc_does_not_inherit_a_previous_reports_violations(tmp_path, stale, monkeypatch):
    _cli_that_writes_nothing(monkeypatch)
    result = stage7_validate._run_drc(tmp_path / "x.kicad_pcb", stale)

    assert not stale.exists()
    assert result["report_json"] is None
    assert "violation_count" not in result
