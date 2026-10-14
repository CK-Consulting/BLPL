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

    def get(self, model, key):
        return None  # no run row: the owner's checkout


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
    # In a run container of its own, never inside the workspace container.
    assert d.calls[0].key.role == "run" and d.boxes == {}
    assert d.calls[0].env == {"BLPL_MODULES_ROOT": "/m"}
    log = runqueue.log_path(tmp_path / "runs", "run_ws").read_text()
    assert log == "emitted\n1 warning\n"


def test_a_failure_to_reach_the_workspace_is_written_to_the_run_log(monkeypatch, tmp_path):
    monkeypatch.setattr(worker_mod, "_LOGS", tmp_path / "runs")
    monkeypatch.setattr(worker_mod, "SessionFactory", _S)

    class Down(FakeDriver):
        async def run_container(self, spec, argv, **kw):
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


def test_asking_for_runs_in_the_shared_workspace_container_refuses_to_start(monkeypatch,
                                                                          caplog):
    """That mode is gone (Codex, PR #20): a config that still asks for it does
    not get the other mode silently, the worker does not start."""
    monkeypatch.setenv("BLPL_RUNS_IN_WORKSPACE", "1")
    monkeypatch.setenv("BLPL_SANDBOX_HOST_PROJECTS", "/srv/blpl/app/data/projects")
    monkeypatch.setenv("BLPL_RUNS_IN_CONTAINER", "0")
    monkeypatch.setattr(worker_mod.serverkey, "load", lambda d: b"k" * 32)
    monkeypatch.setattr(worker_mod.signal, "signal", lambda *a: None)
    with pytest.raises(SystemExit):
        asyncio.run(worker_mod.main())
    assert "shared workspace container" in caplog.text


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
    monkeypatch.setattr(egress, "revoke_run", lambda session, run_id: revoked.append(run_id))
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
    assert revoked == ["run_eg"]  # every grant of the run, by its id


def test_a_reattached_run_gets_no_new_grant_and_its_grants_are_all_revoked(monkeypatch,
                                                                          tmp_path):
    """Codex, PR #20: the retry of a run whose first worker died follows the
    container that worker started, which holds the first grant. The retry
    issues none, and when the run ends every grant of the run id goes."""
    import json

    from app import egress
    from app.sandbox.driver import State
    from app.sandbox.fake import _Box

    class S(_S):
        def get(self, model, key):
            return None

    issued, revoked = [], []
    monkeypatch.setattr(worker_mod, "_LOGS", tmp_path / "runs")
    monkeypatch.setattr(worker_mod, "SessionFactory", S)
    monkeypatch.setattr(worker_mod.runqueue, "heartbeat", lambda s, r: runqueue.RUNNING)
    monkeypatch.setattr(egress, "issue", lambda session, **kw: issued.append(kw) or ("g", "t"))
    monkeypatch.setattr(egress, "revoke_run", lambda session, run_id: revoked.append(run_id))
    monkeypatch.setenv("BLPL_SANDBOX_HTTP_PROXY", "http://squid:3128")
    cmd = [sys.executable, "-m", "blpl.core.cli", "stage1"]
    ws = _workspaces(tmp_path, FakeDriver())
    spec = jobs.run_spec_for(ws.cfg, "pathfinder", "run_re")
    # What the dead worker left: the run's container, started and running.
    ws.driver.boxes[spec.key] = _Box(spec, State.RUNNING, "run-old", argv=cmd,
                                     outcome=(0, [("stdout", b"done\n")]))
    job = runqueue.Claimed(run_id="run_re", project_name="pathfinder", cmd=cmd,
                           env={"HDM_LLM_CHAIN": json.dumps([])})

    assert asyncio.run(ws.run(job)) == 0
    assert ("reattach", spec.key) in ws.driver.ops
    assert issued == [] and revoked == ["run_re"]

