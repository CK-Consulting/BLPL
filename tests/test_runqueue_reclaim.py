"""A worker that dies must not leave a run in flight forever.

The state that was not covered: `request_cancel` only sets a flag, because the
process is in another container and only the worker can finish the transition.
A worker that dies between the flag and the finish leaves the row in
`cancelling` permanently — and `_seal_workspace` refuses to seal a project with
any run in QUEUED, RUNNING or CANCELLING, so one dead container left a project
unable to return to sealed. A real row on example-handheld sat that way for
three days and sixteen hours before anyone noticed, and what gave it away was
the UI still showing an autoroute in progress.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("sqlalchemy")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import runqueue
from app.models import Run


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Run.__table__.create(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


def _run(session, status, *, age_minutes, run_id="run_x"):
    r = Run(
        id=run_id,
        project_id=1,
        user_id=1,
        kind="pipeline stage2->stage8 (core)",
        cmd=["blpl", "run"],
        status=status,
        claimed_by="worker-dead",
        heartbeat_at=runqueue._now() - timedelta(minutes=age_minutes),
        env_nonce=b"x" * 12,
        env_ciphertext=b"sealed",
    )
    session.add(r)
    session.flush()
    return r


def test_a_stale_cancelling_run_is_settled_not_left_in_flight(session):
    r = _run(session, runqueue.CANCELLING, age_minutes=60 * 24 * 3)
    assert runqueue.reclaim_stale(session) == 1
    assert r.status == runqueue.CANCELLED
    assert r.ended_at is not None


def test_a_stale_cancelling_run_is_not_requeued(session):
    """It was deliberately stopped. Putting it back on the queue would restart
    a job somebody asked to end."""
    r = _run(session, runqueue.CANCELLING, age_minutes=60)
    runqueue.reclaim_stale(session)
    assert r.status != runqueue.QUEUED


def test_settling_a_cancel_drops_the_sealed_environment(session):
    """It was sealed for a worker that is gone; nothing will open it now."""
    r = _run(session, runqueue.CANCELLING, age_minutes=60)
    runqueue.reclaim_stale(session)
    assert r.env_nonce is None and r.env_ciphertext is None


def test_settling_a_cancel_records_that_no_exit_was_reported(session):
    r = _run(session, runqueue.CANCELLING, age_minutes=60)
    runqueue.reclaim_stale(session)
    assert r.exit_code == runqueue.INTERRUPTED


def test_a_stale_running_run_still_goes_back_on_the_queue(session):
    """Unfinished work, as distinct from work that was told to stop."""
    r = _run(session, runqueue.RUNNING, age_minutes=60)
    assert runqueue.reclaim_stale(session) == 1
    assert r.status == runqueue.QUEUED
    assert r.claimed_by == "" and r.started_at is None


def test_a_cancelling_run_whose_worker_is_alive_is_left_alone(session):
    """The worker is mid-cancel and will finish it. Stealing the transition
    would race the process that is actually doing the work."""
    r = _run(session, runqueue.CANCELLING, age_minutes=0)
    assert runqueue.reclaim_stale(session) == 0
    assert r.status == runqueue.CANCELLING


def test_a_settled_cancel_no_longer_blocks_sealing(session):
    """The consequence that made this worth fixing.

    _seal_workspace refuses while any run is QUEUED, RUNNING or CANCELLING, so
    a permanently-stuck cancel means a project that can never return to sealed.
    """
    blocking = (runqueue.QUEUED, runqueue.RUNNING, runqueue.CANCELLING)
    r = _run(session, runqueue.CANCELLING, age_minutes=60 * 24 * 3)
    assert r.status in blocking
    runqueue.reclaim_stale(session)
    assert r.status not in blocking
