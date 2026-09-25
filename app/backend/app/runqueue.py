"""The run queue: enqueue here, claim there, stream from the log file.

Three decisions worth stating, because each one avoided a service or a class of
bug.

**Postgres is the queue.** ``SELECT ... FOR UPDATE SKIP LOCKED`` is a queue
Postgres already knows how to be. Two workers reaching for the same row is the
ordinary case rather than a race to defend against: one wins, the other skips.
A broker would have been a second stateful service to run, back up and lose.

**Logs stay files.** They are append-heavy streaming text, which a database is a
poor home for — and a file on a shared volume can be tailed by any process that
can see it. That removes the need for pub/sub entirely, which is why there is no
Redis here despite the fan-out being real.

**Nothing depends on a worker coming back.** A claim is a row edit, not a lock
held in a process, so a worker that dies leaves a row anyone can reclaim after
its heartbeat goes stale. The alternative — a lease only the holder can release —
turns one crashed container into a permanently stuck job.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import vault
from .models import Project, Run, User

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
CANCELLING = "cancelling"
CANCELLED = "cancelled"

# A run whose worker has not checked in for this long is presumed dead and may
# be reclaimed. Generous: a stage that is merely busy must never be stolen from
# a worker that is still working on it.
STALE_AFTER = timedelta(minutes=5)

# What a run that never reported an exit is recorded as. Distinct from any real
# exit code, so "the server went down mid-run" is never mistaken for a stage
# that failed.
INTERRUPTED = -1


class RunActive(RuntimeError):
    """This user already has a run in flight on this project.

    Scoped per user, not per project, because each member now works in their own
    git worktree — two people running stages on one project write to different
    ``.pipeline/`` directories and cannot collide. Before worktrees this had to be
    per project, and a colleague's stage 6 blocked yours: a lock standing in for
    an isolation boundary that did not exist.

    Still per user, because one person running two stages on their own checkout
    would race themselves over the same files.
    """


@dataclass(frozen=True)
class Claimed:
    """A job a worker has taken, with its environment already opened."""

    run_id: str
    project_name: str
    cmd: list[str]
    env: dict[str, str]


def enqueue(
    session: Session,
    project: Project,
    user: User | None,
    kind: str,
    cmd: list[str],
    env: dict[str, str],
    server_key: bytes,
) -> Run:
    """Register a run and hand its environment to whichever worker takes it.

    The environment is sealed here rather than passed live because the worker
    cannot obtain it any other way: it carries the user's provider keys, which
    are sealed under that user's master key and only readable inside their
    unlocked session. This is the moment they are readable, so this is where the
    hand-off happens.
    """
    active = session.scalar(
        select(Run).where(
            Run.project_id == project.id,
            Run.user_id == (user.id if user else None),
            Run.status.in_([QUEUED, RUNNING, CANCELLING]),
        )
    )
    if active is not None:
        raise RunActive(
            f"you already have a run in flight on {project.name!r} ({active.id}); "
            "stop it or wait for it to finish"
        )

    nonce, ciphertext = vault.encrypt_secret(server_key, "run-env", json.dumps(env))
    run = Run(
        id="run_" + uuid.uuid4().hex[:12],
        project_id=project.id,
        user_id=user.id if user else None,
        kind=kind,
        cmd=list(cmd),
        env_nonce=nonce,
        env_ciphertext=ciphertext,
        status=QUEUED,
    )
    session.add(run)
    session.flush()
    return run


def claim_one(session: Session, worker_id: str, server_key: bytes) -> Claimed | None:
    """Take the oldest queued run, or None.

    SKIP LOCKED is what makes several workers safe against each other without a
    broker: a row another transaction is already looking at is passed over
    rather than waited on, so no worker blocks behind a peer.
    """
    # with_for_update rather than raw SQL, because the suite runs on SQLite and
    # production on Postgres. SQLAlchemy renders FOR UPDATE SKIP LOCKED where the
    # dialect has it and omits it where it does not — SQLite has no row locking
    # and a single writer, so omitting it is correct there rather than merely
    # tolerated. Hand-written SQL was a syntax error on SQLite, which showed up
    # as a worker looping silently rather than as a failure.
    run = session.scalars(
        select(Run)
        .where(Run.status == QUEUED)
        .order_by(Run.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()
    if run is None:
        return None
    run.status = RUNNING
    run.claimed_by = worker_id
    run.started_at = _now()
    run.heartbeat_at = _now()
    session.flush()

    env = json.loads(
        vault.decrypt_secret(server_key, "run-env", run.env_nonce, run.env_ciphertext)
    )
    return Claimed(
        run_id=run.id, project_name=run.project.name, cmd=list(run.cmd), env=env
    )


def reclaim_stale(session: Session) -> int:
    """Settle runs whose worker stopped checking in to the queue.

    A claim is a row edit, not a lock held inside a process, precisely so this is
    possible: a container that dies mid-run leaves work anyone can pick up, where
    a lease only its holder could release would leave it stuck forever.

    **The two states settle differently, and that is the point.** A stale
    ``running`` row is work nobody finished, so it goes back on the queue for
    the next worker. A stale ``cancelling`` row is work somebody already asked
    to *stop* — requeueing it would restart a job that was deliberately killed,
    so it is recorded as ``cancelled`` and left alone.

    ``cancelling`` was not covered at all until 2026-09-25, and the omission was
    not cosmetic. ``request_cancel`` sets the flag and returns; only the worker
    can finish the transition. A worker that dies between those two moments
    leaves the row in ``cancelling`` **forever** — the UI shows a run in flight
    days after it stopped, and, worse, ``_seal_workspace`` refuses to seal a
    project with any run in QUEUED, RUNNING or CANCELLING. So one dead container
    left a project permanently unable to return to sealed. A real row on
    example-handheld had been stuck that way for three days and sixteen hours.
    """
    cutoff = _now() - STALE_AFTER
    count = 0

    for run in session.scalars(
        select(Run).where(Run.status == RUNNING, Run.heartbeat_at < cutoff)
    ):
        run.status = QUEUED
        run.claimed_by = ""
        run.started_at = None
        run.heartbeat_at = None
        count += 1

    for run in session.scalars(
        select(Run).where(Run.status == CANCELLING, Run.heartbeat_at < cutoff)
    ):
        run.status = CANCELLED
        run.ended_at = _now()
        if run.exit_code is None:
            run.exit_code = INTERRUPTED
        # The environment was sealed for a worker that is gone; nothing will
        # open it now, and leaving ciphertext behind outlives its purpose.
        run.env_nonce = None
        run.env_ciphertext = None
        count += 1

    session.flush()
    return count


def heartbeat(session: Session, run_id: str) -> str:
    """Say the worker is still alive, and learn whether it should stop.

    One round trip does both, because a worker has to ask something anyway and a
    separate cancellation check would be a second query saying the same thing.
    """
    run = session.get(Run, run_id)
    if run is None:
        return CANCELLED
    run.heartbeat_at = _now()
    session.flush()
    return run.status


def finish(session: Session, run_id: str, exit_code: int) -> None:
    """Record the outcome and forget the environment.

    Clearing the sealed environment is the point of doing it here: the keys it
    carries were needed for the length of the run and no longer, so the window
    in which the row is worth stealing ends with the run.
    """
    run = session.get(Run, run_id)
    if run is None:
        return
    run.status = CANCELLED if run.status == CANCELLING else DONE
    run.exit_code = exit_code
    run.ended_at = _now()
    run.env_nonce = None
    run.env_ciphertext = None
    session.flush()


def request_cancel(session: Session, run_id: str) -> bool:
    """Ask for a run to stop.

    A request, not an act: the process is in another container, so this sets a
    flag its heartbeat will read. Pretending to kill it synchronously would mean
    reporting a death that had not happened yet.
    """
    run = session.get(Run, run_id)
    if run is None or run.status not in (QUEUED, RUNNING):
        return False
    if run.status == QUEUED:
        # Never started, so there is nothing to signal.
        run.status = CANCELLED
        run.ended_at = _now()
        run.env_nonce = None
        run.env_ciphertext = None
    else:
        run.status = CANCELLING
    session.flush()
    return True


def log_path(logs_dir: Path, run_id: str) -> Path:
    return Path(logs_dir) / f"{run_id}.log"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def worker_name() -> str:
    """Identifies a worker in the table.

    Hostname, because in a container the pid is always 1 — every replica called
    itself worker-1, which is exactly useless for the one question this field
    answers: which container is sitting on that job. Nothing depends on it being
    unique, but a name that cannot distinguish two workers is not a name.
    """
    import socket

    return os.environ.get("BLPL_WORKER_ID") or f"worker-{socket.gethostname()}"
