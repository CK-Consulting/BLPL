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
    (tmp_path / "projects" / "pathfinder").mkdir(parents=True, exist_ok=True)
    ws = object.__new__(worker_mod._Workspaces)
    ws.cfg = jobs.WorkspaceRunConfig.from_env({
        "BLPL_SANDBOX_HOST_PROJECTS": "/srv/blpl/app/data/projects",
        "BLPL_PROJECTS_ROOT": str(tmp_path / "projects"),
    })
    ws.driver = driver
    ws.tracker = idle.IdleTracker()
    ws._last_sweep = 0.0
    ws.access = None  # phase 2: docker exec, no agent
    ws.storage = None
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


def test_a_run_gets_its_own_egress_credential_and_it_is_revoked_after(monkeypatch, tmp_path):
    """The grant names the run, carries the hosts of its own chain, reaches
    the run's environment only, and is revoked when the run ends."""
    import json

    from app import egress

    class S(_S):
        def get(self, model, key):
            return None

    issued, revoked = [], []

    def issue(session, **kw):
        issued.append(kw)
        return "g-run", "tok"

    monkeypatch.setattr(worker_mod, "_LOGS", tmp_path / "runs")
    monkeypatch.setattr(worker_mod, "SessionFactory", S)
    monkeypatch.setattr(worker_mod.runqueue, "heartbeat", lambda s, r: runqueue.RUNNING)
    monkeypatch.setattr(egress, "issue", issue)
    monkeypatch.setattr(egress, "revoke", lambda session, name: revoked.append(name))
    monkeypatch.setenv("BLPL_SANDBOX_HTTP_PROXY", "http://squid:3128")
    monkeypatch.setenv("BLPL_FETCH_SERVICE_URL", "http://blpl-fetch:8001")
    chain = [{"provider": "openai-compatible", "endpoint": "mine",
              "base_url": "https://llm.alice.example/v1", "key_env": ""}]
    d = FakeDriver(script=lambda call: (0, []))
    job = runqueue.Claimed(run_id="run_eg", project_name="pathfinder",
                           cmd=[sys.executable, "-m", "blpl.core.cli", "stage1"],
                           env={"HDM_LLM_CHAIN": json.dumps(chain)})

    assert asyncio.run(_workspaces(tmp_path, d).run(job)) == 0

    (kw,) = issued
    assert kw["kind"] == "run" and kw["run_id"] == "run_eg" and kw["project"] == "pathfinder"
    assert kw["hosts"] == ["llm.alice.example"]
    env = d.calls[0].env
    assert env["HTTPS_PROXY"] == "http://g-run:tok@squid:3128"
    assert env["BLPL_FETCH_SERVICE"] == "http://blpl-fetch:8001"
    assert env["BLPL_FETCH_AUTH"] == "g-run:tok"
    assert revoked == ["g-run"]
