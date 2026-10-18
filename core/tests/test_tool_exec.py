"""Chat-tool operations run as a process of their own (blpl.agent.tool_exec)."""

from __future__ import annotations

import json
import subprocess
import sys

from blpl.agent import tool_exec


def test_an_operation_answers_in_an_envelope():
    out = tool_exec.run_op("nope", {})
    assert out == {"ok": False, "kind": "failed", "error": "no tool operation 'nope'"}


def test_a_reference_refusal_is_passed_on_as_one(tmp_path):
    out = tool_exec.run_op("reference_load", {"sch": str(tmp_path / "missing.kicad_sch")})
    assert out["ok"] is False and out["kind"] == "refused" and "not a file" in out["error"]


def test_bad_arguments_fail_without_raising():
    out = tool_exec.run_op("drc", {"unexpected": 1})
    assert out["ok"] is False and out["kind"] == "failed" and "TypeError" in out["error"]


def test_the_process_prints_its_result_last_and_it_is_found(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "blpl.agent.tool_exec", "reference_load",
         json.dumps({"sch": str(tmp_path / "x.kicad_sch")})],
        capture_output=True, text=True, check=False, timeout=60)
    assert proc.returncode == 0
    # A tool's own output may come first; the last marked line is the answer.
    found = tool_exec.result_of("noise\n" + tool_exec.MARK + '{"ok": true, "value": 1}\n' + proc.stdout)
    assert found is not None and found["kind"] == "refused"


def test_output_with_no_result_is_none():
    assert tool_exec.result_of("Traceback ...\nKilled\n") is None
    assert tool_exec.result_of(tool_exec.MARK + "{not json\n") is None
