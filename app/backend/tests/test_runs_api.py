"""Durable run state: runs outlive the request that started them.

Two layers: RunManager unit tests drive real subprocesses on a private event
loop (no pytest-asyncio dependency — scenarios run under asyncio.run); the API
tests go through the client fixture and the real endpoints.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from conftest import running_worker


def _manager(tmp_path: Path):
    from app.runs import RunManager

    return RunManager(tmp_path / "runs.db", tmp_path / "logs")


def _echo_cmd(text: str = "hello") -> list[str]:
    return [sys.executable, "-c", f"print('{text}')"]


def _sleep_cmd(seconds: float = 30) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


async def _drain(mgr, run_id: str) -> tuple[list[str], int | None]:
    lines: list[str] = []
    code: int | None = None
    async for ev, payload in mgr.stream(run_id):
        if ev == "log":
            lines.append(payload["line"])
        elif ev == "done":
            code = payload["exit_code"]
    return lines, code


# -- RunManager ---------------------------------------------------------------


def test_a_run_records_its_log_and_exit_code(tmp_path) -> None:
    async def scenario():
        mgr = _manager(tmp_path)
        rec = mgr.start("proj", "stage0", _echo_cmd("output line"), {})
        lines, code = await _drain(mgr, rec.id)
        assert lines == ["output line"] and code == 0
        # Durable: the row and the log file survive independent of any reader.
        done = mgr.get(rec.id)
        assert done.exit_code == 0 and done.ended_at is not None and not done.running
        assert mgr.log_path(rec.id).read_text() == "output line\n"

    asyncio.run(scenario())


def test_a_finished_run_replays_for_late_readers(tmp_path) -> None:
    async def scenario():
        mgr = _manager(tmp_path)
        rec = mgr.start("proj", "stage0", _echo_cmd("replayed"), {})
        await _drain(mgr, rec.id)
        # Second reader attaches after the fact and still gets everything.
        lines, code = await _drain(mgr, rec.id)
        assert lines == ["replayed"] and code == 0

    asyncio.run(scenario())


def test_a_run_survives_its_reader_disconnecting(tmp_path) -> None:
    """The whole point: abandoning the stream must not kill the subprocess."""

    async def scenario():
        mgr = _manager(tmp_path)
        rec = mgr.start(
            "proj",
            "stage0",
            [sys.executable, "-c", "import time; time.sleep(0.2); print('finished anyway')"],
            {},
        )
        agen = mgr.stream(rec.id)
        await agen.__anext__()  # read only the start event...
        await agen.aclose()     # ...then walk away
        # The run still completes and records.
        for _ in range(50):
            if not (mgr.get(rec.id).running):
                break
            await asyncio.sleep(0.05)
        done = mgr.get(rec.id)
        assert done.exit_code == 0
        assert "finished anyway" in mgr.log_path(rec.id).read_text()

    asyncio.run(scenario())


def test_one_run_per_project_at_a_time(tmp_path) -> None:
    """Stages share .pipeline/ — a second concurrent run would corrupt the
    first's artifacts, so it is refused. A different project is unaffected."""
    from app.runs import RunActive

    import pytest

    async def scenario():
        mgr = _manager(tmp_path)
        rec = mgr.start("proj", "stage0", _sleep_cmd(), {})
        with pytest.raises(RunActive):
            mgr.start("proj", "stage1", _echo_cmd(), {})
        other = mgr.start("other", "stage0", _echo_cmd(), {})
        await _drain(mgr, other.id)
        assert mgr.stop(rec.id) is True
        lines, code = await _drain(mgr, rec.id)
        assert code < 0  # killed (negative signal exit), not completed

    asyncio.run(scenario())


def test_stopping_a_finished_run_is_a_noop(tmp_path) -> None:
    async def scenario():
        mgr = _manager(tmp_path)
        rec = mgr.start("proj", "stage0", _echo_cmd(), {})
        await _drain(mgr, rec.id)
        assert mgr.stop(rec.id) is False

    asyncio.run(scenario())


def test_restart_marks_orphaned_rows_interrupted(tmp_path) -> None:
    """Rows left open by a dead server must not look alive forever."""
    import sqlite3

    async def scenario():
        mgr = _manager(tmp_path)
        rec = mgr.start("proj", "stage0", _echo_cmd(), {})
        await _drain(mgr, rec.id)
        return rec.id

    run_id = asyncio.run(scenario())
    # Simulate a crash mid-run: reopen the row, then boot a fresh manager.
    conn = sqlite3.connect(tmp_path / "runs.db")
    conn.execute("UPDATE run SET ended_at = NULL, exit_code = NULL WHERE id = ?", (run_id,))
    conn.commit()
    conn.close()

    mgr2 = _manager(tmp_path)
    rec = mgr2.get(run_id)
    assert rec.exit_code == -1 and rec.ended_at is not None and not rec.running


# -- API ------------------------------------------------------------------


def _unlock(client) -> None:
    """Sign this client in. The passphrase handshake it used to perform is gone;
    the gate is Clerk now, stubbed in conftest."""
    from conftest import sign_in

    sign_in(client)


def test_a_stage_run_lands_in_history_with_its_log(client) -> None:
    """The API only queues now, so this posts, lets an inline worker execute,
    and then reads the record — which is what the browser does too, except that
    its worker is a container."""
    _unlock(client)
    client.post("/api/projects/init", json={"name": "scratch"})

    with running_worker():
        with client.stream("POST", "/api/projects/scratch/stages/doctor") as r:
            body = "".join(r.iter_text())
    assert "event: done" in body
    assert '"run_id"' in body  # the start event names the run

    listing = client.get("/api/projects/scratch/runs").json()
    assert len(listing) == 1
    run = listing[0]
    assert run["kind"] == "doctor" and run["running"] is False
    assert run["status"] == "done"
    assert run["exit_code"] is not None and run["ended_at"] is not None

    # The full log is durable and fetchable after the stream is long gone.
    log = client.get(f"/api/runs/{run['id']}/log")
    assert log.status_code == 200 and "doctor" in log.text

    # And the stream endpoint replays a finished run end-to-end.
    with client.stream("GET", f"/api/runs/{run['id']}/stream") as r2:
        replay = "".join(r2.iter_text())
    assert "event: done" in replay


def test_runs_endpoints_404_unknown_ids_and_respect_the_session_gate(client) -> None:
    _unlock(client)
    assert client.get("/api/runs/run_nope/log").status_code == 404
    assert client.delete("/api/runs/run_nope").status_code == 404
    # Sign-out is Clerk's, not ours — dropping the token is the same thing from
    # this side of the wire, and is what the gate actually keys on.
    client.headers.pop("Authorization", None)
    assert client.get("/api/projects/scratch/runs").status_code == 401
