"""Diagnostic logging: what every part of the app did, kept where a redeploy cannot reach.

Why it exists: a project re-locked nine times in a working day and nothing
could say when or why. The activity feed kept unseals only, and every
container's stdout — the one place a seal was logged — is deleted when the
container is recreated, which every deploy does. This module writes a
structured log that outlives containers, at levels deep enough to answer
"what was the machine doing when it went wrong" as well as "how is the app
being used and how fast is it".

Levels, deepest first (numbers as Python's ``logging`` orders them):

==========  =====  ==========================================================
TRACE           5  host and process state, sampled: load, memory and cgroup
                   limits, disk free, the process's RSS, threads and open
                   files. The level for "something on the machine is wrong".
DEBUG          10  internals worth reading only when chasing a bug
PERF           15  timings: every API request, every pipeline stage, every
                   run, seal and unseal, client page loads
INFO           20  what happened: runs started and ended, projects sealed
                   and opened, workers reclaiming stale runs
WARNING        30  something was refused or degraded
ERROR          40  something failed; the record carries a host snapshot
CRITICAL       50  a crash; also written by faulthandler to crash.log
==========  =====  ==========================================================

``BLPL_LOG_LEVEL`` (default PERF) sets what is written. Each service writes
``<data>/logs/<service>/<YYYY-MM-DD>.jsonl``, one JSON object per line, and
files older than ``BLPL_LOG_RETAIN_DAYS`` (default 30) are pruned at the day's
first write. stderr keeps the familiar one-line format at INFO and above, so
``docker compose logs`` reads as it did.

Never logged: secrets, keys, tokens, file contents. A record's free text is
the message its call site wrote; structured fields are an allow-list
(``FIELDS``) so a stray ``extra=`` cannot carry a key into the file.
"""

from __future__ import annotations

import datetime as _dt
import faulthandler
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

TRACE = 5
PERF = 15
logging.addLevelName(TRACE, "TRACE")
logging.addLevelName(PERF, "PERF")

#: Structured fields a record may carry (``logger.info(..., extra={...})``).
FIELDS = (
    "event", "project", "run_id", "user_id", "stage", "status", "method", "path",
    "duration_ms", "exit_code", "reason", "idle_min", "limit_min", "bytes", "kind",
    "url", "client", "detail", "count", "ttfb_ms",
)

_DEFAULT_LEVEL = "PERF"
_STARTED = time.time()
#: service -> (log root, its JSON-lines handler), so a second setup with a
#: different root replaces the handler instead of leaving it on the old one.
_configured: dict[str, tuple[Path, "JsonLinesHandler | None"]] = {}


def level_from_env() -> int:
    name = os.environ.get("BLPL_LOG_LEVEL", _DEFAULT_LEVEL).strip().upper() or _DEFAULT_LEVEL
    value = logging.getLevelName(name)
    return value if isinstance(value, int) else logging.getLevelName(_DEFAULT_LEVEL)


def logs_root() -> Path:
    data = Path(os.environ.get("BLPL_DATA_ROOT", "/app/data"))
    return Path(os.environ.get("BLPL_LOG_DIR", str(data / "logs")))


# ------------------------------------------------------------------ host state


def _read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def _meminfo() -> dict[str, int]:
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            try:
                out[key] = int(rest.split()[0]) * 1024
            except (IndexError, ValueError):
                pass
    return out


def _cgroup_memory() -> dict[str, int | str]:
    out: dict[str, int | str] = {}
    for key, path in (("current", "/sys/fs/cgroup/memory.current"),
                      ("max", "/sys/fs/cgroup/memory.max"),
                      ("peak", "/sys/fs/cgroup/memory.peak")):
        raw = _read(path).strip()
        if raw:
            out[key] = int(raw) if raw.isdigit() else raw
    events = _read("/sys/fs/cgroup/memory.events")
    for line in events.splitlines():
        name, _, value = line.partition(" ")
        if name in ("oom", "oom_kill") and value.strip().isdigit():
            out[name] = int(value)
    return out


def _process() -> dict[str, int | float]:
    out: dict[str, int | float] = {"pid": os.getpid(), "uptime_s": round(time.time() - _STARTED)}
    for line in _read("/proc/self/status").splitlines():
        key, _, rest = line.partition(":")
        if key in ("VmRSS", "VmHWM"):
            try:
                out[key.lower() + "_bytes"] = int(rest.split()[0]) * 1024
            except (IndexError, ValueError):
                pass
        elif key == "Threads":
            out["threads"] = int(rest.strip() or 0)
    try:
        out["open_fds"] = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    return out


def host_snapshot() -> dict:
    """The machine and this process, read from /proc and the cgroup: no dependency."""
    snap: dict = {"host": socket.gethostname()}
    try:
        snap["load"] = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        pass
    snap["cpus"] = os.cpu_count()
    snap["memory"] = _meminfo()
    cg = _cgroup_memory()
    if cg:
        snap["cgroup_memory"] = cg
    pressure = _read("/proc/pressure/memory").splitlines()
    if pressure:
        snap["memory_pressure"] = pressure[0]
    disks = {}
    for name, path in (("data", str(logs_root().parent)), ("tmp", "/tmp")):
        try:
            st = os.statvfs(path)
            disks[name] = {"free_bytes": st.f_bavail * st.f_frsize, "size_bytes": st.f_blocks * st.f_frsize}
        except OSError:
            pass
    snap["disk"] = disks
    snap["process"] = _process()
    return snap


# ------------------------------------------------------------------- handlers


class JsonLinesHandler(logging.Handler):
    """One JSON object per line, one file per service per day, old days pruned."""

    def __init__(self, service: str, root: Path | None = None, retain_days: int | None = None):
        super().__init__()
        self.service = service
        self.dir = (root or logs_root()) / service
        self.retain = retain_days if retain_days is not None else int(
            os.environ.get("BLPL_LOG_RETAIN_DAYS", "30"))
        self._day = ""
        self._fh = None
        self._host = socket.gethostname()

    def _file(self, now: _dt.datetime):
        day = now.strftime("%Y-%m-%d")
        if day != self._day or self._fh is None:
            if self._fh is not None:
                self._fh.close()
            self.dir.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.dir / f"{day}.jsonl", "a", encoding="utf-8", buffering=1)
            self._day = day
            self._prune(now)
        return self._fh

    def _prune(self, now: _dt.datetime) -> None:
        cutoff = (now - _dt.timedelta(days=self.retain)).strftime("%Y-%m-%d")
        for f in self.dir.glob("*.jsonl"):
            if f.stem < cutoff:
                try:
                    f.unlink()
                except OSError:
                    pass

    def format_record(self, record: logging.LogRecord) -> dict:
        now = _dt.datetime.fromtimestamp(record.created, _dt.timezone.utc)
        out = {
            "ts": now.isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": getattr(record, "service", None) or self.service,
            "host": self._host,
            "pid": record.process,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in FIELDS:
            if hasattr(record, key):
                out[key] = getattr(record, key)
        if record.exc_info:
            out["exception"] = logging.Formatter().formatException(record.exc_info)
        if record.levelno >= logging.ERROR:
            # What the machine looked like when it failed: the point of a
            # diagnostic log is the cause that is not in the traceback.
            out["host_state"] = host_snapshot()
        elif hasattr(record, "host_state"):
            out["host_state"] = record.host_state
        return out

    def close(self) -> None:
        with self.lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
        super().close()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = json.dumps(self.format_record(record), default=str)
            with self.lock:
                fh = self._file(_dt.datetime.fromtimestamp(record.created, _dt.timezone.utc))
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001 — logging must never take the app down
            self.handleError(record)


def _sampler(interval: float) -> None:
    log = logging.getLogger("blpl.host")
    while True:
        time.sleep(interval)
        if log.isEnabledFor(TRACE):
            log.log(TRACE, "host sample", extra={"event": "host_sample", "host_state": host_snapshot()})


def setup(service: str, *, root: Path | None = None) -> logging.Logger:
    """Configure this process once: stderr as before, plus the JSON-lines file.

    Also installs the crash hooks — uncaught exceptions in any thread go to the
    log at CRITICAL with a host snapshot, and faulthandler writes a hard crash
    (segfault, abort) to ``crash.log`` beside the day's file — and, at TRACE, a
    sampler thread logging the host's state every ``BLPL_LOG_TRACE_INTERVAL``
    seconds (default 30).
    """
    level = level_from_env()
    root_logger = logging.getLogger()
    target = Path(root or logs_root())
    if service in _configured:
        old_root, old_handler = _configured[service]
        if old_root == target:
            return logging.getLogger("blpl")
        # The data root moved (a reload with a new BLPL_DATA_ROOT, as the
        # tests do): a handler left on the old root writes to a file nobody
        # reads, possibly one already deleted (Codex, PR #48).
        if old_handler is not None:
            root_logger.removeHandler(old_handler)
            old_handler.close()
        handler = JsonLinesHandler(service, target)
        handler.setLevel(level)
        root_logger.addHandler(handler)
        _configured[service] = (target, handler)
        return logging.getLogger("blpl")
    root_logger.setLevel(min(level, logging.INFO))
    has_stream = any(isinstance(h, logging.StreamHandler) and not isinstance(h, JsonLinesHandler)
                     for h in root_logger.handlers)
    if not has_stream:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s"))
        stream.setLevel(logging.INFO)
        root_logger.addHandler(stream)
    handler = None
    try:
        handler = JsonLinesHandler(service, target)
        handler.setLevel(level)
        root_logger.addHandler(handler)
        crash = target / service / "crash.log"
        crash.parent.mkdir(parents=True, exist_ok=True)
        faulthandler.enable(open(crash, "a", encoding="utf-8"))  # noqa: SIM115 — lives for the process
    except OSError as exc:
        logging.getLogger("blpl").warning("diagnostic log unavailable: %s", exc)

    def excepthook(exc_type, exc, tb):
        logging.getLogger("blpl.crash").critical(
            "uncaught %s", exc_type.__name__, exc_info=(exc_type, exc, tb),
            extra={"event": "crash"})
        sys.__excepthook__(exc_type, exc, tb)

    def thread_hook(args):
        logging.getLogger("blpl.crash").critical(
            "uncaught %s in thread %s", args.exc_type.__name__,
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback), extra={"event": "crash"})

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    if level <= TRACE:
        interval = float(os.environ.get("BLPL_LOG_TRACE_INTERVAL", "30"))
        threading.Thread(target=_sampler, args=(interval,), name="blpl-host-sampler",
                         daemon=True).start()
    _configured[service] = (target, handler)
    logging.getLogger("blpl").info("diagnostic log: %s at %s", service, logging.getLevelName(level),
                                   extra={"event": "log_start"})
    return logging.getLogger("blpl")


class StageTimer:
    """Wraps a run log's ``write`` and logs, at PERF, how long each stage took.

    The pipeline announces each stage with a ``==> stageN`` line; the time
    between one announcement and the next is that stage's duration. Only the
    headers are read; nothing a stage prints is copied into the diagnostic log.
    """

    def __init__(self, write, *, run_id: str, project: str, logger: logging.Logger | None = None):
        self._write = write
        self._buf = ""
        self._stage: str | None = None
        self._since = time.monotonic()
        self.run_id, self.project = run_id, project
        self.log = logger or logging.getLogger("blpl.perf")

    def write(self, text: str):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            words = line[4:].split() if line.startswith("==> ") else []
            # A stage header is the stage name alone. The pipeline also writes
            # "==> stage7 returned 1; halting", "==> stage6a skipped (...)" and
            # "==> board core": a failure, a skip and a board, none a stage to
            # time (Codex, PR #48). A board closes the stage before it.
            if len(words) == 1:
                self._close()
                self._stage = words[0]
                self._since = time.monotonic()
            elif words and words[0] == "board":
                self._close()
        return self._write(text)

    def _close(self, status: str = "") -> None:
        if self._stage is None:
            return
        ms = round((time.monotonic() - self._since) * 1000)
        self.log.log(PERF, "stage %s took %d ms", self._stage, ms, extra={
            "event": "stage", "stage": self._stage, "duration_ms": ms,
            "run_id": self.run_id, "project": self.project, **({"status": status} if status else {})})
        self._stage = None

    def close(self, status: str = "") -> None:
        self._close(status)
