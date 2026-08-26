"""The worker must see a cancel even when the stage says nothing.

The wedge that forced this: stage1 sat inside an unbounded request to a local
model that had stopped answering. The stage printed nothing, and the worker's
loop only woke on output — so it never heartbeat, never saw `cancelling`, and
the stop button read as broken while both sides waited forever. run_81123250333a,
cancelled by hand from inside the container.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from app import worker as worker_mod
from app import runqueue


class _Claimed:
    run_id = "run_test"
    cmd = [sys.executable, "-c", "import time; time.sleep(300)"]  # silent forever
    env = None


def test_a_silent_hung_stage_is_cancelled_within_a_heartbeat(monkeypatch, tmp_path) -> None:
    """Silence must tick the same clock output does. Before the fix this test
    hangs for the sleep's full five minutes; with it, the worker notices the
    cancel on the first silent interval and the process dies."""
    monkeypatch.setattr(worker_mod, "_HEARTBEAT_EVERY", 0.3)
    monkeypatch.setattr(worker_mod, "_LOGS", tmp_path)

    beats = []

    def fake_heartbeat(session, run_id):
        beats.append(run_id)
        return runqueue.CANCELLING

    monkeypatch.setattr(worker_mod.runqueue, "heartbeat", fake_heartbeat)

    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass

    monkeypatch.setattr(worker_mod, "SessionFactory", _S)

    async def run():
        return await asyncio.wait_for(worker_mod._run_job(_Claimed()), timeout=15)

    code = asyncio.run(run())

    assert beats, "the worker never heartbeat while the stage was silent"
    assert code != 0  # terminated, not a clean exit


def test_output_still_streams_and_finishes_normally(monkeypatch, tmp_path) -> None:
    """The bounded read must not break the ordinary case."""
    monkeypatch.setattr(worker_mod, "_HEARTBEAT_EVERY", 0.3)
    monkeypatch.setattr(worker_mod, "_LOGS", tmp_path)
    monkeypatch.setattr(worker_mod.runqueue, "heartbeat", lambda s, r: runqueue.RUNNING)

    class _S:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def commit(self): pass

    monkeypatch.setattr(worker_mod, "SessionFactory", _S)

    class _C(_Claimed):
        cmd = [sys.executable, "-c", "print('alpha'); print('beta')"]

    code = asyncio.run(asyncio.wait_for(worker_mod._run_job(_C()), timeout=15))

    assert code == 0
    log = (tmp_path / [p.name for p in tmp_path.iterdir()][0]).read_text() if list(tmp_path.iterdir()) else ""
    assert "alpha" in log and "beta" in log
