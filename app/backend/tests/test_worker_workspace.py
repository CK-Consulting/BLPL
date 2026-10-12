"""The worker, with BLPL_RUNS_IN_WORKSPACE=1, runs through the driver.

Same shape as test_worker_cancel.py: the database is faked at the two calls
the worker makes, and the driver is the in-memory one.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("psycopg")  # app.db builds its engine at import, as in the image

from app import runqueue  # noqa: E402
from app import worker as worker_mod  # noqa: E402
from app.sandbox import idle, jobs  # noqa: E402
from app.sandbox.fake import FakeDriver  # noqa: E402


class _S:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        pass


def _workspaces(tmp_path, driver) -> worker_mod._Workspaces:
    ws = object.__new__(worker_mod._Workspaces)
    ws.cfg = jobs.WorkspaceRunConfig.from_env({
        "BLPL_SANDBOX_HOST_PROJECTS": "/srv/blpl/app/data/projects",
        "BLPL_PROJECTS_ROOT": str(tmp_path / "projects"),
    })
    ws.driver = driver
    ws.tracker = idle.IdleTracker()
    ws._last_sweep = 0.0
    ws.access = None  # phase 2: docker exec, no agent
    return ws


def test_a_run_goes_through_the_workspace_and_lands_in_the_log(monkeypatch, tmp_path):
    monkeypatch.setattr(worker_mod, "_LOGS", tmp_path / "runs")
    monkeypatch.setattr(worker_mod, "SessionFactory", _S)
    monkeypatch.setattr(worker_mod.runqueue, "heartbeat", lambda s, r: runqueue.RUNNING)
    d = FakeDriver(script=lambda call: (4, [("stdout", b"emitted\n"), ("stderr", b"1 warning\n")]))
    job = runqueue.Claimed(run_id="run_ws", project_name="pathfinder",
                           cmd=[sys.executable, "-m", "blpl.core.cli", "stage6"],
                           env={"BLPL_SERVER_KEY": "k", "BLPL_MODULES_ROOT": "/m"})

    code = asyncio.run(_workspaces(tmp_path, d).run(job))

    assert code == 4
    assert d.calls[0].env == {"BLPL_MODULES_ROOT": "/m"}
    log = runqueue.log_path(tmp_path / "runs", "run_ws").read_text()
    assert log == "emitted\n1 warning\n"


def test_a_failure_to_reach_the_workspace_is_written_to_the_run_log(monkeypatch, tmp_path):
    monkeypatch.setattr(worker_mod, "_LOGS", tmp_path / "runs")
    monkeypatch.setattr(worker_mod, "SessionFactory", _S)

    class Down(FakeDriver):
        async def ensure(self, spec):
            raise RuntimeError("proxy socket not found")

    job = runqueue.Claimed(run_id="run_down", project_name="pathfinder", cmd=["x"], env={})
    with pytest.raises(RuntimeError):
        asyncio.run(_workspaces(tmp_path, Down()).run(job))
    log = runqueue.log_path(tmp_path / "runs", "run_down").read_text()
    assert "could not run in the project's workspace: proxy socket not found" in log


def test_misconfigured_isolation_refuses_to_start_rather_than_falling_back(monkeypatch):
    monkeypatch.setenv("BLPL_RUNS_IN_WORKSPACE", "1")
    monkeypatch.delenv("BLPL_SANDBOX_HOST_PROJECTS", raising=False)
    monkeypatch.setattr(worker_mod.serverkey, "load", lambda d: b"k" * 32)
    # main() installs SIGTERM/SIGINT handlers; keep them off the test process.
    monkeypatch.setattr(worker_mod.signal, "signal", lambda *a: None)
    with pytest.raises(SystemExit):
        asyncio.run(worker_mod.main())
