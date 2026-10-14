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
from .sandbox import idle as sandbox_idle
from .sandbox import jobs as sandbox_jobs

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


class _Workspaces:
    """Phase 2: runs execute in per-project workspaces, not in this container.

    Off unless BLPL_RUNS_IN_WORKSPACE=1. When on, the driver talks to the
    socket proxy, never the daemon, and this process stops spawning stages.
    It still claims, heartbeats and logs exactly as before, so the API cannot
    tell the difference except that runs no longer see this container's
    environment or files.
    """

    SWEEP_EVERY = 60.0

    def __init__(self, server_key: bytes | None = None) -> None:
        from .sandbox import workspaces
        from .sandbox.docker import DockerConfig, DockerDriver

        self.cfg = sandbox_jobs.WorkspaceRunConfig.from_env()
        self.driver = DockerDriver(DockerConfig.from_env())
        self.tracker = sandbox_idle.IdleTracker()
        self._last_sweep = 0.0
        # Phase 4: with BLPL_STORAGE=btrfs, every run in a container is
        # preceded by a snapshot of the project, taken by the storage helper.
        self.storage = None
        if os.environ.get("BLPL_STORAGE") == "btrfs":
            from .sandbox.btrfs import HelperClient, HelperStorage

            self.storage = HelperStorage(
                HelperClient(os.environ.get("BLPL_STORAGE_SOCKET",
                                            "/run/blpl-storage/storage.sock")),
                host_root=os.environ.get("BLPL_SANDBOX_HOST_PROJECTS", ""))
        # Phase 3: with BLPL_FILES_VIA_WORKSPACE=1 the workspace runs its agent,
        # and runs go through it rather than through docker exec.
        self.access = None
        if workspaces.enabled():
            if server_key is None:
                raise ValueError("phase 3 needs the server key to derive agent tokens")
            self.access = workspaces.Access.from_env(
                server_key, lambda: DockerDriver(DockerConfig.from_env()))

    def _grant(self, job: runqueue.Claimed) -> tuple[dict[str, str], str | None]:
        """This run's egress credential and fetch-service access (egress.py).

        The grant reaches the defaults, the administrators' hosts, and the
        hosts of the LLM endpoints in this run's own chain; another member's
        run gets its own. Nothing without an egress proxy."""
        from . import egress
        from .models import Run

        if not os.environ.get("BLPL_SANDBOX_HTTP_PROXY"):
            return {}, None
        with SessionFactory() as session:
            row = session.get(Run, job.run_id)
            name, token = egress.issue(
                session, kind="run", project=job.project_name, run_id=job.run_id,
                user_id=row.user_id if row is not None else None,
                hosts=egress.run_hosts(job.env))
            session.commit()
        env = egress.proxy_env(name, token)
        service = os.environ.get("BLPL_FETCH_SERVICE_URL", "")
        if service:
            env.update(BLPL_FETCH_SERVICE=service, BLPL_FETCH_AUTH=f"{name}:{token}")
        return env, name

    def _revoke(self, name: str | None) -> None:
        if name is None:
            return
        from . import egress

        try:
            with SessionFactory() as session:
                egress.revoke(session, name)
                session.commit()
        except Exception:  # noqa: BLE001 - it expires on its own
            logger.exception("could not revoke egress grant %s", name)

    def _member(self, job: runqueue.Claimed) -> str | None:
        """Whose checkout this run works in: None for the project's owner (or
        a run nobody started), ``u<id>`` for a member's worktree. A run
        container mounts that checkout and no other."""
        from sqlalchemy import select

        from .models import Project, Run

        with SessionFactory() as session:
            row = session.get(Run, job.run_id)
            if row is None or row.user_id is None:
                return None
            owner = session.scalar(select(Project.owner_id).where(Project.id == row.project_id))
            return None if owner == row.user_id else f"u{row.user_id}"

    async def run(self, job: runqueue.Claimed) -> int:
        grant_env, grant = self._grant(job)
        try:
            return await self._run(job, grant_env)
        finally:
            self._revoke(grant)

    async def _run(self, job: runqueue.Claimed, grant_env: dict[str, str]) -> int:
        _LOGS.mkdir(parents=True, exist_ok=True)
        path = runqueue.log_path(_LOGS, job.run_id)
        logger.info("running %s in workspace for %s (%s)", job.run_id, job.project_name,
                    job.cmd[:3])

        async def poll() -> str:
            with SessionFactory() as session:
                status = runqueue.heartbeat(session, job.run_id)
                session.commit()
            return status

        with path.open("w", encoding="utf-8", buffering=1) as log:
            try:
                if sandbox_jobs.runs_in_containers() or self.access is not None:
                    # A container per run, .git read-only in it. Always so with
                    # the agent in use: a run inside the agent's own container
                    # could take over its socket (Codex, PR #20). The agent is
                    # told a run is active so writes through it wait.
                    access = self.access

                    async def mark(active: bool, ttl: float) -> None:
                        if access is None:
                            return
                        client = await access.ensure(job.project_name)
                        try:
                            await asyncio.to_thread(client.run_mark, job.run_id, active, ttl)
                        finally:
                            client.close()

                    snapshot = None
                    if self.storage is not None:
                        storage = self.storage

                        async def snapshot(label: str) -> object:
                            return await asyncio.to_thread(
                                storage.snapshot, job.project_name, label)

                    return await sandbox_jobs.run_job_in_container(
                        self.driver, self.cfg, project_name=job.project_name,
                        run_id=job.run_id, cmd=job.cmd, env=job.env, log=log.write,
                        poll=poll, mark=mark, poll_every=_HEARTBEAT_EVERY,
                        snapshot=snapshot, extra_inject=grant_env,
                        member=self._member(job),
                    )
                return await sandbox_jobs.run_job(
                    self.driver, self.cfg, project_name=job.project_name,
                    cmd=job.cmd, env=job.env, log=log.write, poll=poll,
                    poll_every=_HEARTBEAT_EVERY, tracker=self.tracker, extra_inject=grant_env,
                )
            except Exception as exc:  # noqa: BLE001 - recorded in the run's own log
                # The member reads this log, so say what failed in the
                # workspace rather than leaving an empty log and a bare -1.
                log.write(f"blpl: could not run in the project's workspace: {exc}\n")
                raise

    async def sweep_if_due(self) -> None:
        now = asyncio.get_running_loop().time()
        if now - self._last_sweep < self.SWEEP_EVERY:
            return
        self._last_sweep = now
        try:
            activity = None
            if self.access is not None:
                access = self.access

                async def activity(key):
                    return await access.agent_idle(key.container_name())
            stopped = await sandbox_idle.sweep(
                self.driver, self.tracker, idle_after_s=self.cfg.idle_after_s,
                activity=activity,
            )
            if stopped:
                logger.info("stopped %d idle workspace(s)", len(stopped))
        except Exception:  # noqa: BLE001 - a sweep failing must not stop the worker
            logger.exception("idle sweep failed")


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

    # The worker always has the project mount, so it is where blobs written by
    # older versions into projects/.sealed are moved to the blob directory a
    # mountless backend reads (workspace.migrate_sealed; idempotent).
    try:
        from . import workspace

        moved = workspace.migrate_sealed(
            Path(os.environ.get("BLPL_PROJECTS_ROOT", str(_DATA / "projects"))))
        if moved.moved or moved.conflicts:
            logger.info("sealed blobs: moved %s; conflicts kept in place %s",
                        moved.moved, moved.conflicts)
    except Exception:  # noqa: BLE001 - never a reason not to run stages
        logger.exception("could not move sealed blobs")
    # A backend without the project mount cannot create these, and a
    # workspace needs both of its bind sources to exist.
    try:
        made = sandbox_jobs.ensure_worktree_dirs(
            Path(os.environ.get("BLPL_PROJECTS_ROOT", str(_DATA / "projects"))))
        if made:
            logger.info("created worktree directories for %s", made)
    except OSError:
        logger.exception("could not create worktree directories")

    # Documents for runs in workspaces, fetched here (fetchsvc.py) on the
    # workspaces' network, since a workspace has no general internet.
    if (listen := os.environ.get("BLPL_FETCH_LISTEN")) and sandbox_jobs.enabled():
        from . import fetchsvc

        fetchsvc.serve(listen, _DATA / "fetch-held")

    workspaces = None
    if sandbox_jobs.enabled():
        try:
            workspaces = _Workspaces(server_key)
        except ValueError as exc:
            # Misconfigured isolation must not quietly fall back to running
            # stages in here, with every secret this container holds.
            logger.error("BLPL_RUNS_IN_WORKSPACE=1 but %s; refusing to start", exc)
            sys.exit(1)

    worker_id = runqueue.worker_name()
    logger.info("worker %s ready (%s)", worker_id,
                "runs in workspaces" if workspaces else "runs in this container")

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
            if workspaces is not None and workspaces.access is None:
                # With the agent (phase 3+) the backend sweeps, because only it
                # holds the keys to seal what it stops.
                await workspaces.sweep_if_due()
            await asyncio.sleep(_IDLE_POLL)
            continue

        code = runqueue.INTERRUPTED
        try:
            code = await (workspaces.run(job) if workspaces else _run_job(job))
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
