"""The per-project gate between starting containers and sealing (sandbox/gate.py).

flock semantics, verified rather than assumed: a seal never queues in the
kernel for the exclusive lock (it polls with LOCK_NB), so a shared holder that
takes the gate again for a nested operation is never stuck behind a seal
that is waiting. And a shared holder never asks for the exclusive lock.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.sandbox import gate


def test_a_nested_shared_take_is_not_blocked_by_a_waiting_seal(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    sealing = {}

    def seal():
        t0 = time.monotonic()
        try:
            with gate.held(root, "p", exclusive=True, wait_s=1.5):
                sealing["got"] = True
        except gate.GateBusy:
            sealing["busy_after"] = time.monotonic() - t0

    with gate.held(root, "p", exclusive=False, wait_s=1):        # the request
        t = threading.Thread(target=seal)
        t.start()
        time.sleep(0.2)                                          # the seal is waiting now
        t0 = time.monotonic()
        with gate.held(root, "p", exclusive=False, wait_s=1):    # a nested agent call
            nested = time.monotonic() - t0
        t.join(5)
    assert nested < 0.2, "the nested shared take waited behind the seal"
    assert "got" not in sealing and sealing["busy_after"] >= 1.4  # busy, not deadlocked


def test_a_seal_gets_the_gate_once_operations_finish(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    release = threading.Event()

    def operation():
        with gate.held(root, "p", exclusive=False, wait_s=1):
            release.wait(5)

    t = threading.Thread(target=operation)
    t.start()
    time.sleep(0.1)
    with pytest.raises(gate.GateBusy):
        with gate.held(root, "p", exclusive=True, wait_s=0.2):
            pass
    release.set()
    t.join(5)
    with gate.held(root, "p", exclusive=True, wait_s=1):
        pass


def test_a_start_waits_for_a_seal_and_projects_are_independent(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    with gate.held(root, "p", exclusive=True, wait_s=1):
        with pytest.raises(gate.GateBusy):
            with gate.held(root, "p", exclusive=False, wait_s=0.2):
                pass
        with gate.held(root, "q", exclusive=False, wait_s=0.2):  # another project: free
            pass
    with pytest.raises(ValueError):
        with gate.held(root, "../p", exclusive=False, wait_s=0.1):
            pass
