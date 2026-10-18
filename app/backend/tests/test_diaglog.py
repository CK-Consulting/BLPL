"""The diagnostic log: levels, files that outlive containers, and what goes in them."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import diaglog  # noqa: E402


def _handler(tmp_path, level=diaglog.TRACE):
    h = diaglog.JsonLinesHandler("svc", root=tmp_path, retain_days=30)
    h.setLevel(level)
    log = logging.getLogger(f"blpl.test.{tmp_path.name}")
    log.setLevel(diaglog.TRACE)
    log.addHandler(h)
    log.propagate = False
    return log, h


def _lines(tmp_path):
    files = sorted((tmp_path / "svc").glob("*.jsonl"))
    return [json.loads(line) for f in files for line in f.read_text().splitlines()]


def test_levels_are_ordered_trace_debug_perf_info():
    assert diaglog.TRACE < logging.DEBUG < diaglog.PERF < logging.INFO
    assert logging.getLevelName(diaglog.PERF) == "PERF"
    assert logging.getLevelName(diaglog.TRACE) == "TRACE"


def test_records_are_json_lines_with_allowed_fields_only(tmp_path):
    log, _ = _handler(tmp_path)
    log.info("sealed p", extra={"event": "seal", "project": "p", "secret_key": "sk-123"})
    (rec,) = _lines(tmp_path)
    assert rec["event"] == "seal" and rec["project"] == "p" and rec["level"] == "INFO"
    assert "secret_key" not in rec and "sk-123" not in json.dumps(rec)


def test_an_error_carries_the_hosts_state(tmp_path):
    log, _ = _handler(tmp_path)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("failed")
    (rec,) = _lines(tmp_path)
    assert rec["level"] == "ERROR" and "RuntimeError: boom" in rec["exception"]
    state = rec["host_state"]
    assert "memory" in state and "process" in state and state["process"]["pid"] > 0


def test_the_level_filters_what_is_written(tmp_path):
    log, _ = _handler(tmp_path, level=diaglog.PERF)
    log.log(diaglog.TRACE, "deep")
    log.debug("debug")
    log.log(diaglog.PERF, "timed", extra={"event": "request", "duration_ms": 12})
    assert [r["msg"] for r in _lines(tmp_path)] == ["timed"]


def test_old_days_are_pruned(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "2000-01-01.jsonl").write_text("{}\n")
    log, _ = _handler(tmp_path)
    log.info("today")
    assert not (tmp_path / "svc" / "2000-01-01.jsonl").exists()


def test_stage_timer_times_each_stage_from_the_headers_alone(tmp_path):
    log, _ = _handler(tmp_path)
    written = []
    t = diaglog.StageTimer(written.append, run_id="r1", project="p", logger=log)
    t.write("==> stage0-det\nsome output with a SECRET\n==> stage1\n")
    t.write("more\n")
    t.close()
    recs = [r for r in _lines(tmp_path) if r.get("event") == "stage"]
    assert [r["stage"] for r in recs] == ["stage0-det", "stage1"]
    assert all(r["run_id"] == "r1" and "duration_ms" in r for r in recs)
    assert "SECRET" not in json.dumps(recs)
    assert "".join(written).count("==>") == 2  # the run log itself is untouched


def _backend_records():
    import app.main  # noqa: F401 — sets the backend's log up
    root = diaglog.logs_root() / "backend"
    return [json.loads(line) for f in sorted(root.glob("*.jsonl")) for line in f.read_text().splitlines()]


def test_every_api_request_is_timed_by_route_template(client):
    from conftest import init_body, sign_in

    sign_in(client)
    client.post("/api/projects/init", json=init_body("scratch"))
    client.get("/api/projects/scratch/git/status")
    recs = [r for r in _backend_records() if r.get("event") == "request"]
    paths = {r["path"] for r in recs}
    assert "/api/projects/{project_id}/git/status" in paths
    assert all("scratch" not in r["path"] for r in recs)
    assert all(isinstance(r["duration_ms"], int) for r in recs)


def test_a_browser_error_lands_in_the_log_bounded(client):
    r = client.post("/api/diag/client", json={"events": [
        {"kind": "error", "level": "error", "message": "x is undefined", "stack": "at f (a.js:1)",
         "url": "http://app/projects/p"}]})
    assert r.json()["kept"] == 1
    rec = [x for x in _backend_records() if x.get("event") == "client_error"][-1]
    assert rec["service"] == "frontend" and rec["msg"].endswith("x is undefined")
    assert rec["detail"] == "at f (a.js:1)"


def test_setup_follows_a_moved_log_root(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    diaglog.setup("moving", root=a)
    diaglog.setup("moving", root=b)
    logging.getLogger("blpl.moving").info("after the move", extra={"event": "moved"})
    found_b = [json.loads(x) for f in (b / "moving").glob("*.jsonl") for x in f.read_text().splitlines()]
    assert any(r.get("event") == "moved" for r in found_b)
    found_a = [json.loads(x) for f in (a / "moving").glob("*.jsonl") for x in f.read_text().splitlines()]
    assert not any(r.get("event") == "moved" for r in found_a)


def test_browser_budgets_are_per_forwarded_client(client):
    import app.main as main

    main._client_budget.clear()
    ev = {"events": [{"kind": "error", "level": "error", "message": "m"}] * 20}
    for _ in range(3):
        client.post("/api/diag/client", json=ev, headers={"X-Real-IP": "10.0.0.1"})
    # One browser used its 60; another, behind the same nginx, still has its own.
    assert client.post("/api/diag/client", json=ev, headers={"X-Real-IP": "10.0.0.1"}).json()["kept"] == 0
    assert client.post("/api/diag/client", json=ev, headers={"X-Real-IP": "10.0.0.2"}).json()["kept"] == 20
