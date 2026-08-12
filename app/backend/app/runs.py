"""Durable run state: every stage invocation is a record that outlives the tab.

Before this, a run existed only as an open HTTP response: close the laptop mid
stage6 and the server killed the subprocess and forgot it ever started — the
"what did that run do" question had no answer unless you watched it live. Now a
run is a first-class thing:

  - a row in runs.db (project, what ran, when, exit code), written at start and
    finished at exit — including exits nobody watched;
  - a full log on disk under the data dir, appended as the subprocess speaks;
  - a live fan-out, so any number of SSE readers can attach — the tab that
    started it, a second browser, or a reader that connects after a refresh —
    and each gets the whole log from the top and then the live tail.

The subprocess's lifetime is bound to the *run*, not to the request that
started it. A client disconnect changes nothing server-side; stopping is an
explicit, recorded act (``stop()``). One run per project at a time — stages
write into the same .pipeline/ directory, so two concurrent runs would corrupt
each other's artifacts; the second start is refused, not queued.

Restart honesty: rows still open when the server comes back up are closed as
interrupted (exit_code -1) rather than left looking alive forever.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    kind        TEXT NOT NULL,               -- 'stage6', 'pipeline stage0→stage8', …
    cmd         TEXT NOT NULL,               -- json argv; safe, keys ride in the env
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at    TEXT,
    exit_code   INTEGER
);
CREATE INDEX IF NOT EXISTS run_project ON run(project, started_at DESC);
"""

# The exit code recorded when a run's real exit was never observed: a row left
# open by a dead server, or a subprocess that failed to launch. A run killed
# via stop() records its true negative signal exit (e.g. -9) instead. Stage
# exit codes proper are >= 0, so "negative" reliably means "did not complete".
INTERRUPTED = -1


class RunActive(RuntimeError):
    """The project already has a live run. Stages share .pipeline/, so a second
    concurrent run would silently corrupt the first's artifacts."""


@dataclass(frozen=True)
class RunRecord:
    id: str
    project: str
    kind: str
    cmd: list[str]
    started_at: str
    ended_at: str | None
    exit_code: int | None
    running: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project": self.project,
            "kind": self.kind,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "running": self.running,
        }


class _LiveRun:
    """In-flight state for one run: the process, the line buffer, subscribers.

    ``publish`` appends to the buffer and fans out to every queue in one
    synchronous step — no await between them — so a subscriber that snapshots
    the buffer length and then attaches its queue can never miss or duplicate
    a line (the event loop cannot interleave a publish into that window).
    """

    def __init__(self, run_id: str, project: str):
        self.run_id = run_id
        self.project = project
        self.proc: asyncio.subprocess.Process | None = None
        self.lines: list[str] = []
        self.queues: set[asyncio.Queue] = set()
        self.exit_code: int | None = None

    def publish(self, line: str) -> None:
        self.lines.append(line)
        for q in self.queues:
            q.put_nowait(line)

    def finish(self, code: int) -> None:
        self.exit_code = code
        for q in self.queues:
            q.put_nowait(None)  # sentinel: no more lines


class RunManager:
    def __init__(self, db_path: Path, logs_dir: Path):
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # Rows still open are runs the previous server process took down with
        # it. Close them as interrupted so nothing looks alive forever.
        self._conn.execute(
            "UPDATE run SET ended_at = datetime('now'), exit_code = ? WHERE ended_at IS NULL",
            (INTERRUPTED,),
        )
        self._conn.commit()
        self._live: dict[str, _LiveRun] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self, project: str, kind: str, cmd: list[str], env: dict[str, str]) -> RunRecord:
        """Register the run and launch its subprocess as a background task.

        The task — not the caller's HTTP response — owns the subprocess from
        here: it pumps output to the log file and live subscribers, and writes
        the exit row even if every client has long since disconnected.
        """
        for lr in self._live.values():
            if lr.project == project:
                raise RunActive(
                    f"project {project!r} already has a run in flight ({lr.run_id}); "
                    "stop it or wait for it to finish"
                )
        run_id = "run_" + uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO run (id, project, kind, cmd) VALUES (?, ?, ?, ?)",
            (run_id, project, kind, json.dumps(cmd)),
        )
        self._conn.commit()
        live = _LiveRun(run_id, project)
        self._live[run_id] = live
        asyncio.get_running_loop().create_task(self._pump(live, cmd, env))
        return self.get(run_id)  # type: ignore[return-value]  # just inserted

    async def _pump(self, live: _LiveRun, cmd: list[str], env: dict[str, str]) -> None:
        code = INTERRUPTED
        log_path = self.log_path(live.run_id)
        try:
            with log_path.open("w", encoding="utf-8") as log:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
                live.proc = proc
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace").rstrip("\n")
                    log.write(line + "\n")
                    log.flush()
                    live.publish(line)
                code = await proc.wait()
        except FileNotFoundError as exc:
            # The one launch failure worth a readable trace in the log: the
            # interpreter/module isn't there. Recorded, not raised — the run
            # row is the error report.
            log_path.write_text(f"failed to launch: {exc}\n", encoding="utf-8")
            live.publish(f"failed to launch: {exc}")
        finally:
            self._conn.execute(
                "UPDATE run SET ended_at = datetime('now'), exit_code = ? WHERE id = ?",
                (code, live.run_id),
            )
            self._conn.commit()
            live.finish(code)
            self._live.pop(live.run_id, None)

    def stop(self, run_id: str) -> bool:
        """Kill a live run. Returns False when it isn't live (already done)."""
        live = self._live.get(run_id)
        if live is None or live.proc is None or live.proc.returncode is not None:
            return False
        live.proc.kill()
        return True

    # -- inspection ----------------------------------------------------------

    def get(self, run_id: str) -> RunRecord | None:
        row = self._conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
        return self._to_record(row) if row else None

    def list_for_project(self, project: str, limit: int = 50) -> list[RunRecord]:
        rows = self._conn.execute(
            "SELECT * FROM run WHERE project = ? ORDER BY started_at DESC, id DESC LIMIT ?",
            (project, limit),
        ).fetchall()
        return [self._to_record(r) for r in rows]

    def log_path(self, run_id: str) -> Path:
        return self.logs_dir / f"{run_id}.log"

    def _to_record(self, row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            project=row["project"],
            kind=row["kind"],
            cmd=json.loads(row["cmd"]),
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            exit_code=row["exit_code"],
            running=row["id"] in self._live,
        )

    # -- streaming -----------------------------------------------------------

    async def stream(self, run_id: str):
        """Yield ('start'|'log'|'done', payload) — full replay, then live tail.

        Works identically for a finished run (replay from the log file, then
        done) and a live one (replay the buffer, then follow). This is what
        makes a browser refresh mid-run a non-event: reattach and catch up.
        """
        rec = self.get(run_id)
        if rec is None:
            return
        yield "start", {"cmd": rec.cmd, "run_id": rec.id}

        live = self._live.get(run_id)
        if live is None:
            log_path = self.log_path(run_id)
            if log_path.exists():
                for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    yield "log", {"line": line}
            yield "done", {"exit_code": rec.exit_code, "run_id": rec.id}
            return

        # Attach before replaying: publish() appends to the buffer and the
        # queues without an intervening await, so buffer[:n] + queue is exactly
        # the whole stream — no gap, no duplicate.
        q: asyncio.Queue = asyncio.Queue()
        live.queues.add(q)
        try:
            for line in live.lines[: len(live.lines)]:
                yield "log", {"line": line}
            while True:
                item = await q.get()
                if item is None:
                    break
                yield "log", {"line": item}
            yield "done", {"exit_code": live.exit_code, "run_id": run_id}
        finally:
            live.queues.discard(q)
