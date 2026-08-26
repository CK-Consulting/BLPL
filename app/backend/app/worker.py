"""The process that actually runs stages.

This is the container with KiCad in it. The API no longer executes anything —
it enqueues, and one of these picks the job up — which is what lets the API run
more than one process without SSE readers landing on a worker that cannot see
the run they asked about.

Deliberately dull. It claims a job, spawns the subprocess, writes every line to
the log file, checks in, and records the exit. No HTTP, no session, no user: by
the time work reaches here, every permission question has already been answered
by the API, and the environment it needs was sealed into the job at dispatch.

Run with:  python -m app.worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from . import runqueue, serverkey
from .db import SessionFactory

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
)
logger = logging.getLogger("blpl.worker")

_DATA = Path(os.environ.get("BLPL_DATA_ROOT", "/app/data"))
_LOGS = _DATA / "runs"
# How long to wait when there is nothing to do. Short enough that a run does not
# sit visibly queued, long enough that an idle deployment is not hammering the
# database. Polling rather than LISTEN/NOTIFY because a worker already has to
# wake up to send heartbeats and reclaim stale rows.
_IDLE_POLL = 1.0
_HEARTBEAT_EVERY = 20.0

_stopping = False


def _handle_signal(*_a) -> None:
    """Stop taking new work, but let the current run finish.

    Killing a stage mid-write leaves .pipeline/ half-updated, which is worse
    than a slightly slower shutdown — a partially written artefact looks real to
    everything downstream.
    """
    global _stopping
    logger.info("shutdown requested; will stop after the current run")
    _stopping = True


async def _run_job(job: runqueue.Claimed) -> int:
    _LOGS.mkdir(parents=True, exist_ok=True)
    path = runqueue.log_path(_LOGS, job.run_id)
    last_beat = 0.0

    logger.info("running %s (%s)", job.run_id, job.cmd[:3])
    with path.open("w", encoding="utf-8", buffering=1) as log:
        proc = await asyncio.create_subprocess_exec(
            *job.cmd,
            env=job.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None

        async def beat() -> str:
            with SessionFactory() as session:
                status = runqueue.heartbeat(session, job.run_id)
                session.commit()
            return status

        # The read is bounded by the heartbeat interval, and that bound is the
        # whole fix for a real wedge: this loop used to be `async for raw in
        # proc.stdout`, which only wakes when the stage PRINTS something. A
        # stage stuck in a network call prints nothing — so the worker never
        # heartbeat, never saw `cancelling`, and the stop button read as
        # broken while both sides waited on a nemotron reply that was never
        # coming. Silence now ticks the same clock output does.
        while True:
            try:
                raw = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=_HEARTBEAT_EVERY
                )
            except asyncio.TimeoutError:
                raw = None
            if raw:
                # Line-buffered and flushed as it goes: a reader is tailing
                # this file, so anything held back is a log that appears to
                # stall.
                log.write(raw.decode("utf-8", "replace"))
            elif raw == b"":
                break  # EOF: the process closed stdout; collect it below.

            now = asyncio.get_running_loop().time()
            if raw is None or now - last_beat > _HEARTBEAT_EVERY:
                last_beat = now
                if await beat() == runqueue.CANCELLING:
                    logger.info("%s was cancelled; terminating", job.run_id)
                    proc.terminate()
                    try:
                        # A process hung hard enough to need cancelling may be
                        # hung hard enough to ignore SIGTERM.
                        return await asyncio.wait_for(proc.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        logger.warning("%s ignored SIGTERM; killing", job.run_id)
                        proc.kill()
                        return await proc.wait()
        return await proc.wait()


async def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    server_key = serverkey.load(_DATA)
    if server_key is None:
        # Without it the sealed environment cannot be opened, so every job would
        # fail one at a time. Better to refuse to start and say why.
        logger.error(
            "no server key at %s and BLPL_SERVER_KEY unset — cannot open job environments",
            serverkey.key_path(_DATA),
        )
        sys.exit(1)

    worker_id = runqueue.worker_name()
    logger.info("worker %s ready", worker_id)

    while not _stopping:
        try:
            with SessionFactory() as session:
                job = runqueue.claim_one(session, worker_id, server_key)
                session.commit()
        except Exception:  # noqa: BLE001 — a database blip must not kill the worker
            logger.exception("could not claim a job")
            await asyncio.sleep(_IDLE_POLL * 5)
            continue

        if job is None:
            # Nothing waiting. Take the opportunity to return work abandoned by
            # a worker that died, which nobody else is going to do.
            try:
                with SessionFactory() as session:
                    freed = runqueue.reclaim_stale(session)
                    session.commit()
                if freed:
                    logger.info("reclaimed %d stale run(s)", freed)
            except Exception:  # noqa: BLE001
                logger.exception("could not reclaim stale runs")
            await asyncio.sleep(_IDLE_POLL)
            continue

        code = runqueue.INTERRUPTED
        try:
            code = await _run_job(job)
        except Exception:  # noqa: BLE001
            logger.exception("run %s failed to execute", job.run_id)
        finally:
            # Always recorded, even when the run blew up — a row left RUNNING
            # forever is the state that makes a project permanently unrunnable,
            # since another run cannot start while one is in flight.
            with SessionFactory() as session:
                runqueue.finish(session, job.run_id, code)
                session.commit()
            logger.info("run %s finished with %s", job.run_id, code)

    logger.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    asyncio.run(main())
