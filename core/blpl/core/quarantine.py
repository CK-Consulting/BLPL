"""Where retrieved files land, and what has to be true before they leave.

Two things in a BLPL project arrive from outside it: a datasheet fetched from a
distributor, and a file a member uploads (a vendor reference design, an
application note, a datasheet that was never fetched). Everything else is
written by the people working on the board or generated from what they wrote. That makes retrieved files a category
of their own, and the point of this module is to keep them in that category
rather than letting them quietly become project files.

    project/
      retrieved/                 ← arrives here, untrusted, and stays until it passes
        quarantine.json          ← the ledger: what came in, from where, and what was found
        <sha256[:12]>-<mpn>.pdf
      datasheets/                ← only files that have passed live here
      references/                ← uploads that are not datasheets, once passed
      libraries/                 ← uploaded KiCad symbols, footprints and 3D models,
                                   once passed *and* parsed (see library_upload)

The two-directory shape does the work. Code that reads ``datasheets/`` is
reading files that were inspected and — where a scanner exists — scanned. Code
that reads ``retrieved/`` knows exactly what it is touching. Nothing has to
remember a policy, because the path says it.

**What "passed" means.** Two independent checks, answering different questions:

* ``pdf_inspect`` reads what the file will *do*. This is the check that matters
  for the thing a datasheet has no business doing — opening a network
  connection when someone opens it. A file carrying an ``/OpenAction`` is held
  no matter what any scanner says, because a scanner's silence only means the
  file is not a *known* threat.
* ``av`` asks whether the file is known-bad. Optional, external, and honest
  about its absence: no scanner yields ``unscanned``, never ``clean``.

**A held file is kept, not deleted.** It is evidence: which part, which
distributor, what was in it. Deleting it would destroy the only record of an
event worth knowing about, and would make the same download happen again
tomorrow with nobody the wiser.

**An upload may be any type, and the ledger says which were not inspected.**
``pdf_inspect`` answers a PDF-shaped question. A ``.step``, a ``.zip`` or a
vendor ``.xlsx`` has no inspector here, so for an upload it is released with
its inspection recorded as ``not_inspected`` rather than held forever or
described as checked. The scanner still gates it, and anything whose bytes are
a PDF is inspected as one whatever its name says. A distributor fetch keeps the
stricter rule: it only ever expects a PDF, so anything else is held.

**The ledger is append-mostly and never lies by omission.** Every retrieval gets
an entry whatever the outcome, so "no entry" means "never fetched" rather than
"fetched and fine".
"""

from __future__ import annotations

import errno
import fcntl
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import secrets
import threading
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import av, library_upload, pdf_inspect

QUARANTINE_DIRNAME = "retrieved"
TRUSTED_DIRNAME = "datasheets"
REFERENCES_DIRNAME = "references"
LEDGER_NAME = "quarantine.json"

# Where an upload of each kind lands once it passes. The uploader picks the
# kind, so the directory name keeps saying what is in it: a datasheet goes where
# the extraction tools already look, KiCad library files go where the resolver
# looks, and anything else goes beside them.
UPLOAD_KINDS = {
    "datasheet": TRUSTED_DIRNAME,
    "reference": REFERENCES_DIRNAME,
    "symbol": library_upload.SYMBOLS_DIR,
    "footprint": library_upload.FOOTPRINTS_DIR,
    "3d_model": library_upload.MODELS_DIR,
    # A vendor zip: its library files go to the three above, its documents to
    # references/<zip name>/, and anything else stays in the zip in retrieved/.
    "kicad_bundle": library_upload.LIBRARIES_DIRNAME,
}

# The kinds that are placed into a project library and so need one named.
LIBRARY_KINDS = frozenset({"symbol", "footprint", "3d_model", "kicad_bundle"})

# How a file arrived. Recorded on every entry, because "who put this here" is
# the first question anyone asks about an untrusted file.
DISTRIBUTOR = "distributor"
UPLOAD = "upload"

# Inspection state for a file no inspector here understands. Distinct from
# `cannot_inspect`, which means an inspector tried and failed.
NOT_INSPECTED = "not_inspected"

# Held, released, or rejected. A file is *held* by default and only moves on a
# decision that was recorded.
HELD = "held"
RELEASED = "released"

# What the checks found, in one word, for anything that has to decide whether
# the browser may render a file rather than only whether it may be released.
# The two questions differ: an unscanned file may be released (see
# `require_scan`), but "nothing known-bad was found" and "nobody looked" are
# not the same claim, and only the first is `CLEAN`.
CLEAN = "clean"            # inspected, nothing active, and a scanner said clean
UNSCANNED = "unscanned"    # inspected, nothing active, but no scanner ran
# HELD and NOT_INSPECTED, above, are the other two.

# The inspector a verdict came from. See pdf_inspect.VERSION.
INSPECTOR = f"pdf_inspect/{pdf_inspect.VERSION}"

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

logger = logging.getLogger(__name__)

# Who hears about a verdict the moment it is reached, in this process. The app
# keeps verdicts in its database, keyed by content hash, and decides from them
# whether a browser may render a file; the ledger beside the file is committed
# project content that a pull can rewrite, so it cannot be the source. A sink
# set here gets the record straight from the inspection that produced it.
# A context variable rather than a module-level hook, so that it belongs to
# the request or chat turn that set it (and to the threads it hands work to)
# rather than to whichever project last set it.
_verdict_sink: ContextVar[Callable[["Record"], None] | None] = ContextVar(
    "quarantine_verdict_sink", default=None
)


@contextmanager
def recording_verdicts(sink: Callable[["Record"], None] | None) -> Iterator[None]:
    """Hand every record `accept` produces in this context to ``sink``.

    ``None`` is allowed and records nothing, so a caller with no database can
    pass what it has.
    """
    token = _verdict_sink.set(sink)
    try:
        yield
    finally:
        _verdict_sink.reset(token)


def _tell_sink(rec: "Record") -> None:
    sink = _verdict_sink.get()
    if sink is None:
        return
    try:
        sink(rec)
    except Exception:  # noqa: BLE001
        # Not raised. A verdict that failed to be recorded leaves the file
        # without one, which is the safe state (it downloads rather than
        # renders); failing the fetch over it would lose the file as well.
        logger.exception("recording the verdict for %s failed", rec.sha256)


def quarantine_dir(project_dir: str | Path) -> Path:
    return Path(project_dir) / QUARANTINE_DIRNAME


def trusted_dir(project_dir: str | Path) -> Path:
    return Path(project_dir) / TRUSTED_DIRNAME


def _now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe(text: str, fallback: str = "file") -> str:
    cleaned = _SAFE.sub("_", (text or "").strip()).strip("._-")
    return cleaned[:64] or fallback


@dataclass
class Record:
    """One retrieval: what arrived, from where, and what was decided."""

    sha256: str
    filename: str                    # inside retrieved/
    bytes: int
    mpn: str = ""
    distributor: str = ""
    source_url: str = ""
    retrieved_at: str = field(default_factory=_now)
    state: str = HELD
    released_as: str = ""            # filename inside released_to
    released_to: str = TRUSTED_DIRNAME
    inspection: dict = field(default_factory=dict)
    scan: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    # Provenance for uploads. Defaults describe a distributor fetch, so ledger
    # rows written before these fields existed still read correctly.
    origin: str = DISTRIBUTOR
    uploaded_by: str = ""
    original_name: str = ""
    kind: str = "datasheet"
    llm_ignore: bool = False
    # Which inspector produced `inspection`. Empty on rows written before it
    # was recorded, which is itself an answer: not this inspector.
    inspector: str = ""
    # Library uploads: the library it went into, and every path it wrote —
    # a bundle writes several, a merged symbol writes into a shared file.
    library: str = ""
    placed: list[str] = field(default_factory=list)
    # Every library these bytes were placed into. One row per hash, and the
    # same vendor file uploaded to two libraries is placed in both.
    libraries: list[str] = field(default_factory=list)
    # The symbol or footprint names it brought, so removing one by name can
    # say which upload it came from.
    names: list[str] = field(default_factory=list)
    # The same names, by the library they were placed in. One row can hold
    # one set of bytes placed in several libraries, and removing a symbol from
    # one of them must not forget it is still in the others.
    names_by_library: dict[str, list[str]] = field(default_factory=dict)
    # A bundle's verdict per entry: name, kind, state, path, reasons.
    members: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def verdict(self) -> str:
        return verdict_of(self.to_dict())


# -- the file system, without following links -----------------------------------------
#
# A project directory may be a checkout a run can write to. A run that plants a
# link at a name quarantine writes, `datasheets/LM317.pdf` say, or at a
# directory on the way (`references` -> another member's worktree), would have
# an upload or a fetched file written through it, wherever it pointed. So every
# directory below the project root is opened by descriptor, one component at a
# time, refusing a link; every file is read without following one; and every
# write goes to a fresh name created exclusively in that directory and is then
# renamed over a target checked not to be a link. A rename replaces a link
# rather than writing through it, so a link planted between the check and the
# rename is replaced, not followed (Codex, PR #20).


class LinkRefused(ValueError):
    """A link, or something that is not a plain file or directory, where
    quarantine reads or writes. Refused, and nothing written."""


_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _open_at(name: str, dirfd: int, flags: int, *, want_dir: bool, where: str) -> int:
    """Open name in dirfd without following a link, whatever the kernel does.

    Under gVisor (runsc), O_DIRECTORY | O_NOFOLLOW follows a link to a
    directory, where Linux refuses it. So the decision is lstat's, which both
    report alike, and the descriptor must be the inode lstat saw; a link
    swapped in between is refused, not followed (as the workspace agent's
    open_at). FileNotFoundError when nothing is there."""
    before = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or want_dir != stat.S_ISDIR(before.st_mode) or \
            (not want_dir and not stat.S_ISREG(before.st_mode)):
        raise LinkRefused(f"{where}/{name} is a link or not a plain "
                          f"{'directory' if want_dir else 'file'}; quarantine does not use it")
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | _O_CLOEXEC |
                     (os.O_DIRECTORY if want_dir else 0), dir_fd=dirfd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise LinkRefused(f"{where}/{name} is a link; quarantine does not use it") from None
        raise
    try:
        after = os.fstat(fd)
        now = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or \
                (now.st_dev, now.st_ino) != (after.st_dev, after.st_ino):
            raise LinkRefused(f"{where}/{name} changed while it was being opened")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _component(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\0" in name:
        raise ValueError(f"{name!r} is not a single file or directory name")
    return name


@contextmanager
def _dir(project_dir: str | Path, dirname: str, *, create: bool) -> Iterator[int | None]:
    """A descriptor for ``project_dir/dirname``, each component opened without
    following a link. None when it does not exist and ``create`` is false.

    The project root itself is the caller's: it is opened as given."""
    parts = [_component(p) for p in dirname.split("/") if p]
    if create:
        # The root is the caller's, created as before if it is not there yet.
        os.makedirs(project_dir, exist_ok=True)
    try:
        fd = os.open(str(project_dir), os.O_RDONLY | os.O_DIRECTORY | _O_CLOEXEC)
    except FileNotFoundError:
        if create:
            raise
        yield None
        return
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                nxt = _open_at(part, fd, os.O_RDONLY, want_dir=True, where=dirname)
            except FileNotFoundError:
                if create:
                    raise
                yield None
                return
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


def _kind(dirfd: int, name: str) -> int | None:
    """The file type at name, not following a link; None when absent."""
    try:
        return stat.S_IFMT(os.stat(name, dir_fd=dirfd, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return None


def _refuse_unless_plain(dirfd: int, name: str, where: str) -> bool:
    """Whether a plain file is at name (False when nothing is); a link or
    anything else there is refused."""
    kind = _kind(dirfd, name)
    if kind is None:
        return False
    if kind != stat.S_IFREG:
        raise LinkRefused(f"{where}/{name} is a link or not a plain file; refusing to use it")
    return True


def _read(dirfd: int, name: str, where: str) -> bytes | None:
    """A plain file's bytes, None when it is absent, refused when a link."""
    try:
        fd = _open_at(_component(name), dirfd, os.O_RDONLY, want_dir=False, where=where)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _write(dirfd: int, name: str, data: bytes, where: str) -> None:
    """Put data at name in dirfd: written to a fresh name created exclusively
    there, then renamed over name, which must not be a link."""
    _component(name)
    _refuse_unless_plain(dirfd, name, where)
    tmp = f".{name[:64]}.{secrets.token_hex(8)}.part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC,
                 0o644, dir_fd=dirfd)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        _refuse_unless_plain(dirfd, name, where)
        os.rename(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dirfd)
        except OSError:
            pass
        raise


def _load_doc(project_dir: str | Path) -> dict:
    """The ledger document, read without following a link; {} when absent."""
    with _dir(project_dir, QUARANTINE_DIRNAME, create=False) as qfd:
        if qfd is None:
            return {}
        raw = _read(qfd, LEDGER_NAME, QUARANTINE_DIRNAME)
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load(project_dir: str | Path) -> list[dict]:
    return _load_doc(project_dir).get("retrieved", [])


def _load_events(project_dir: str | Path) -> list[dict]:
    events = _load_doc(project_dir).get("events", [])
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def _save(project_dir: str | Path, rows: list[dict], events: list[dict] | None = None) -> None:
    # Events are appended, never rewritten: a row save carries them over as
    # they are on disk.
    events = _load_events(project_dir) if events is None else events
    doc: dict = {"schema_version": 1, "updated": _now(), "retrieved": rows}
    if events:
        doc["events"] = events
    body = json.dumps(doc, indent=2)
    # Write-then-move, as everywhere else here: a crash mid-write must not leave
    # a ledger that cannot be parsed, because an unreadable ledger reads as "no
    # files were ever retrieved".
    with _dir(project_dir, QUARANTINE_DIRNAME, create=True) as qfd:
        _write(qfd, LEDGER_NAME, (body + "\n").encode("utf-8"), QUARANTINE_DIRNAME)


# ------------------------------------------------------------------ transactions
#
# The ledger is read, changed and written back whole, and a library placement
# reads the library it merges into. Two of those at once in one checkout (the
# workspace agent serves each request on its own thread; uvicorn may run
# several workers; the chat's datasheet fetch writes the same ledger) built
# the second write from a read that predated the first: a symbol merged into
# a flat library by one upload vanished under the other's, or a ledger row was
# dropped and a vendor file lost its untrusted framing (Codex, PR #32). So
# every change to the ledger, and the placement or move it records, holds the
# checkout's quarantine lock for the whole of it.

LOCK_NAME = "quarantine.lock"
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()


def _lock_path(project_dir: str | Path) -> Path:
    """In the checkout's private git directory, as llm_ignore's lock, so it is
    never committed and each member's worktree has its own. For a checkout
    that is not a repository, a file in the temporary directory named for its
    path: nothing is created in the checkout itself, where a refused upload
    must leave no trace."""
    project_dir = Path(project_dir)
    dot_git = project_dir / ".git"
    try:
        if dot_git.is_dir():
            return dot_git / LOCK_NAME
        if dot_git.is_file():
            line = dot_git.read_text(encoding="utf-8").strip()
            if line.startswith("gitdir:"):
                gitdir = Path(line[len("gitdir:"):].strip())
                if not gitdir.is_absolute():
                    gitdir = project_dir / gitdir
                if gitdir.is_dir():
                    return gitdir / LOCK_NAME
    except OSError:
        pass
    import tempfile

    key = hashlib.sha256(str(project_dir.resolve()).encode()).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"blpl-{key}.{LOCK_NAME}"


@contextmanager
def locked(project_dir: str | Path) -> Iterator[None]:
    """Hold the checkout's quarantine lock for one whole transaction.

    ``flock``, so processes exclude each other, and a threading lock beside
    it for threads of one process. Re-entrant within a thread: a bundle's
    accept_upload accepts each of its entries, and a route that holds the lock
    across a move calls the ledger functions that take it too.
    """
    path = str(_lock_path(project_dir).resolve())
    held = getattr(_HELD, "paths", None)
    if held is None:
        held = _HELD.paths = {}
    if held.get(path):
        held[path] += 1
        try:
            yield
        finally:
            held[path] -= 1
        return
    with _LOCKS_GUARD:
        tlock = _LOCKS.setdefault(path, threading.Lock())
    with tlock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            held[path] = 1
            try:
                yield
            finally:
                held.pop(path, None)
                fcntl.flock(fh, fcntl.LOCK_UN)


def _transaction(fn):
    """Run fn under ``locked`` for its ``project_dir`` argument."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        project_dir = sig.bind(*args, **kwargs).arguments["project_dir"]
        with locked(project_dir):
            return fn(*args, **kwargs)
    return wrapper


def record_for(project_dir: str | Path, sha256: str) -> dict | None:
    for row in _load(project_dir):
        if row.get("sha256") == sha256:
            return row
    return None


def _released_paths(row: dict) -> list[str]:
    """Every path a row says it put in the project."""
    out = [p for p in row.get("placed") or [] if isinstance(p, str)]
    if row.get("state") == RELEASED and row.get("released_as"):
        path = f"{row.get('released_to') or TRUSTED_DIRNAME}/{row['released_as']}"
        if path not in out:
            out.append(path)
    return out


def _names_by_library(row: dict) -> dict[str, list[str]]:
    """A row's names per library; an older row knew only its one library."""
    by_lib = row.get("names_by_library")
    if isinstance(by_lib, dict) and by_lib:
        return {str(k): list(v or []) for k, v in by_lib.items()}
    if row.get("library") and row.get("names"):
        return {row["library"]: list(row["names"])}
    return {}


def _upsert(project_dir: str | Path, rec: Record) -> None:
    """Record ``rec``, the one row for its hash, without forgetting a placement.

    Rows are keyed by content, so the same bytes accepted again replace the
    earlier row. Replacing it outright dropped every path the earlier
    acceptance had put in the project: a vendor symbol uploaded to library A
    and then to library B left A's file on disk with no row claiming it, and
    the agent then read A's copy as the project's own text rather than as
    untrusted vendor content. Every path the earlier row released is carried
    into the new one, as are the libraries it went into.
    """
    rows = _load(project_dir)
    for old in rows:
        if old.get("sha256") != rec.sha256:
            continue
        mine = set(_released_paths(rec.to_dict()))
        rec.placed = list(rec.placed) + [
            p for p in _released_paths(old) if p not in mine and p not in rec.placed]
        mine_by_lib = {k: list(v) for k, v in (rec.names_by_library or {}).items()}
        for lib, names in _names_by_library(old).items():
            have = mine_by_lib.setdefault(lib, [])
            have.extend(n for n in names if n not in have)
        rec.names_by_library = mine_by_lib
        rec.names = list(rec.names) + [n for n in old.get("names") or [] if n not in rec.names]
        libs = list(old.get("libraries") or ([old["library"]] if old.get("library") else []))
        rec.libraries = libs
    if rec.library and rec.library not in rec.libraries:
        rec.libraries = [*rec.libraries, rec.library]
    rows = [r for r in rows if r.get("sha256") != rec.sha256]
    rows.append(rec.to_dict())
    rows.sort(key=lambda r: (r.get("retrieved_at", ""), r.get("sha256", "")))
    _save(project_dir, rows)


def verdict(inspection_state: str, scan_state: str, reasons: list[str]) -> str:
    """One of CLEAN, UNSCANNED, HELD or NOT_INSPECTED.

    Any reason at all is HELD, whatever else is true. An inspection that did
    not finish is never promoted to anything better than HELD, even with no
    reasons recorded, because "could not tell" must not read as "fine".
    """
    if reasons:
        return HELD
    if inspection_state == NOT_INSPECTED:
        return NOT_INSPECTED
    if inspection_state != "inspected":
        return HELD
    return CLEAN if scan_state == "clean" else UNSCANNED


def verdict_of(row: dict) -> str:
    """The verdict a ledger row describes. See `verdict`."""
    return verdict(
        (row.get("inspection") or {}).get("state", ""),
        (row.get("scan") or {}).get("state", ""),
        list(row.get("reasons") or []),
    )


def require_scan() -> bool:
    """Whether an unscanned file may be released.

    Default false, so a deployment with no scanner still works and says so in
    the ledger. Set ``BLPL_QUARANTINE_REQUIRE_SCAN=1`` to hold anything that no
    scanner has seen — the right setting once a scanner is actually running,
    and the wrong one before, because it would hold everything.
    """
    return os.environ.get("BLPL_QUARANTINE_REQUIRE_SCAN", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _decide(
    inspection: pdf_inspect.Inspection, scan: av.ScanResult, *, require: bool = False
) -> list[str]:
    """Why this file may not be released. Empty means it may.

    ``require`` adds to the environment's setting and never relaxes it: a
    caller that knows a scan is required where this process's environment
    does not say so (an agent in a workspace started before the policy was
    forwarded to it) can insist, and nothing can switch the setting off.
    """
    reasons: list[str] = []

    if scan.state == "infected":
        reasons.append(f"a malware scanner matched {scan.signature or 'a signature'}")
    if scan.state in ("unscanned", "error") and (require or require_scan()):
        reasons.append(
            f"no scanner has checked this file ({scan.detail or scan.state}), and "
            "this deployment requires a scan before release"
        )

    if inspection.state == "not_a_pdf":
        reasons.append(inspection.reason)
    elif inspection.state == "cannot_inspect":
        reasons.append(inspection.reason or "the file could not be inspected")
    for finding in inspection.active:
        # Deduplicated on the sentence, not the construct: `/JavaScript` and
        # `/JS` are two names for one hazard and reading "it carries JavaScript"
        # twice makes the list look padded. Both are still in the ledger.
        reason = f"it {finding.detail}"
        if reason not in reasons:
            reasons.append(reason)
    return reasons


@dataclass
class Assessment:
    """What quarantine would decide about some bytes, without filing them."""

    inspection: pdf_inspect.Inspection
    scan: av.ScanResult
    reasons: list[str]
    inspector: str = INSPECTOR

    @property
    def verdict(self) -> str:
        return verdict(self.inspection.state, self.scan.state, self.reasons)


def assess(data: bytes, *, is_pdf: bool) -> Assessment:
    """Inspect, scan and decide, exactly as `accept` does, without storing.

    For files that arrive by another door than this module — a chat attachment
    goes into the conversation store, not ``retrieved/`` — so that the same
    rules decide whether a model may read them. One policy, two front doors:
    the alternative was an attachment path that took an encrypted PDF the
    upload quarantine had already held, and handed it to the model.
    """
    if is_pdf or _looks_like_pdf(data):
        inspection = pdf_inspect.inspect_bytes(data)
    else:
        inspection = pdf_inspect.Inspection(
            state=NOT_INSPECTED, findings=[], reason="no inspector for this type"
        )
    scan = av.scan_bytes(data)
    return Assessment(
        inspection=inspection,
        scan=scan,
        reasons=_decide(inspection, scan),
        inspector=INSPECTOR if inspection.state != NOT_INSPECTED else "",
    )


def looks_like_pdf(data: bytes) -> bool:
    """Whether a PDF reader would take these bytes for a PDF, whatever the name.

    The spec allows junk before the header and readers look within the first
    1 KB, so this does too: anything narrower would let a file with a byte of
    padding in front be treated as "not a PDF" by us and as one by the viewer.
    """
    return b"%PDF-" in data[:1024]


_looks_like_pdf = looks_like_pdf


@_transaction
def accept(
    data: bytes,
    project_dir: str | Path,
    *,
    mpn: str = "",
    distributor: str = "",
    source_url: str = "",
    suffix: str = ".pdf",
    origin: str = DISTRIBUTOR,
    uploaded_by: str = "",
    original_name: str = "",
    kind: str = "datasheet",
    llm_ignore: bool = False,
    insist_on_scan: bool = False,
) -> Record:
    """Take delivery of a retrieved file: store, inspect, scan, decide.

    ``insist_on_scan`` holds an unscanned file even where
    ``BLPL_QUARANTINE_REQUIRE_SCAN`` is not set here; see ``_decide``.

    Returns the record with its verdict. The bytes are written into
    ``retrieved/`` before anything else happens, so a file that crashes the
    inspector is still on disk to look at afterwards.

    An ``UPLOAD`` of a type no inspector here understands is recorded as
    ``not_inspected`` and may be released; a distributor fetch of one is held.
    """
    project_dir = Path(project_dir)

    digest = hashlib.sha256(data).hexdigest()
    # Content-addressed, with the part number kept for a human reading the
    # directory. The hash leads so two files for one MPN cannot collide and so
    # nothing here can be mistaken for a finished, trusted artifact.
    label = mpn or Path(original_name).stem
    name = f"{digest[:12]}-{_safe(label, 'retrieved')}{suffix}"
    with _dir(project_dir, QUARANTINE_DIRNAME, create=True) as qfd:
        # Content-addressed: a plain file already at the name holds these bytes.
        if not _refuse_unless_plain(qfd, name, QUARANTINE_DIRNAME):
            _write(qfd, name, data, QUARANTINE_DIRNAME)

    if suffix.lower() == ".pdf" or _looks_like_pdf(data):
        # The bytes in hand, not the name: a name in a run's checkout is the
        # run's to replace.
        inspection = pdf_inspect.inspect_bytes(data)
    elif origin == UPLOAD:
        inspection = pdf_inspect.Inspection(
            state=NOT_INSPECTED,
            findings=[],
            reason=f"no inspector for {suffix or 'extensionless'} files; stored as uploaded",
        )
    else:
        inspection = pdf_inspect.Inspection(
            state="cannot_inspect",
            findings=[],
            reason=f"no inspector for {suffix} files",
        )
    scan = av.scan_bytes(data)
    reasons = _decide(inspection, scan, require=insist_on_scan)

    rec = Record(
        sha256=digest,
        filename=name,
        bytes=len(data),
        mpn=mpn,
        distributor=distributor,
        source_url=source_url,
        state=HELD,
        inspection=inspection.to_dict(),
        scan=scan.to_dict(),
        reasons=reasons,
        origin=origin,
        uploaded_by=uploaded_by,
        original_name=original_name,
        kind=kind,
        llm_ignore=llm_ignore,
        inspector=INSPECTOR if inspection.state != NOT_INSPECTED else "",
    )
    _upsert(project_dir, rec)
    _tell_sink(rec)
    return rec


@_transaction
def release(
    project_dir: str | Path, rec: Record, *, as_name: str = "", dest_dirname: str = TRUSTED_DIRNAME
) -> Record:
    """Copy a passed file into ``datasheets/`` (or ``dest_dirname``) and record that it moved.

    A different file already holding the destination name is not overwritten:
    the new one takes a hash suffix instead. Two vendors' ``app-note.pdf`` are
    two files, and silently replacing one with the other would lose it.

    Refuses on a held file rather than trusting the caller to have checked. The
    check is cheap and the failure it prevents — a file with an ``/OpenAction``
    sitting in the directory whose name means "these are fine" — is not.
    """
    if rec.reasons:
        raise ValueError(
            f"{rec.filename} is held and cannot be released: " + "; ".join(rec.reasons)
        )
    project_dir = Path(project_dir)
    with _dir(project_dir, QUARANTINE_DIRNAME, create=False) as qfd:
        data = _read(qfd, rec.filename, QUARANTINE_DIRNAME) if qfd is not None else None
    if data is None:
        raise ValueError(f"{rec.filename} is not in {QUARANTINE_DIRNAME}/")
    if hashlib.sha256(data).hexdigest() != rec.sha256:
        # Replaced since it was accepted: what was assessed is not this.
        raise ValueError(f"{QUARANTINE_DIRNAME}/{rec.filename} changed since it was accepted")
    name = _component(as_name or f"{_safe(rec.mpn, 'datasheet')}.pdf")
    with _dir(project_dir, dest_dirname, create=True) as dfd:
        existing = _read(dfd, name, dest_dirname)
        if existing is not None and hashlib.sha256(existing).hexdigest() != rec.sha256:
            stem, dot, ext = name.rpartition(".")
            name = (f"{stem}-{rec.sha256[:8]}.{ext}" if dot and stem
                    else f"{name}-{rec.sha256[:8]}")
        _write(dfd, name, data, dest_dirname)

    rec.state = RELEASED
    rec.released_as = name
    rec.released_to = dest_dirname
    _upsert(project_dir, rec)
    return rec


@_transaction
def accept_and_release(
    data: bytes,
    project_dir: str | Path,
    *,
    mpn: str = "",
    distributor: str = "",
    source_url: str = "",
    as_name: str = "",
    dest_dirname: str = TRUSTED_DIRNAME,
) -> Record:
    """The whole path for a fetched datasheet, in the order it has to happen."""
    rec = accept(
        data, project_dir, mpn=mpn, distributor=distributor, source_url=source_url
    )
    if not rec.reasons:
        rec = release(project_dir, rec, as_name=as_name, dest_dirname=dest_dirname)
    return rec


# Where a file held by another quarantine is kept, as its ledger row says.
HELD_ELSEWHERE = "control-plane"


@_transaction
def note_held_elsewhere(
    project_dir: str | Path,
    *,
    sha256: str,
    size: int,
    reasons: list[str],
    inspection: dict | None = None,
    scan: dict | None = None,
    mpn: str = "",
    distributor: str = "",
    source_url: str = "",
) -> Record:
    """Record a file that another quarantine held, without its bytes.

    A fetch made for a workspace goes through the control plane's quarantine
    (blpl.core.fetcher). A file it holds never reaches the workspace, so there
    is nothing to put in ``retrieved/``; the ledger still says it was fetched
    and why it was held, so "no entry" keeps meaning "never fetched". The
    evidence stays where it was held."""
    rec = Record(
        sha256=sha256, filename="", bytes=size, mpn=mpn, distributor=distributor,
        source_url=source_url, state=HELD, inspection=dict(inspection or {}),
        scan=dict(scan or {}), reasons=list(reasons) or ["held by the control plane"],
        released_to=HELD_ELSEWHERE,
    )
    _upsert(project_dir, rec)
    return rec


def _upload_suffix(original_name: str) -> str:
    """The extension the uploader's filename claims, made safe to put on disk."""
    suffix = Path(original_name or "").suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9_]{1,15}", suffix) else ""


@_transaction
def accept_upload(
    data: bytes,
    project_dir: str | Path,
    *,
    original_name: str,
    kind: str,
    uploaded_by: str,
    mpn: str = "",
    llm_ignore: bool = False,
    library: str = "",
    insist_on_scan: bool = False,
) -> Record:
    """The whole path for a file a member uploads, in the order it has to happen.

    Released into the directory for its ``kind``: a datasheet with an MPN is
    named after the part, the way a fetched one is, so the resolver finds it;
    a KiCad library file is parsed and placed into project library ``library``
    (see ``library_upload``); anything else keeps the uploader's filename, made
    safe.

    Library files pass the same inspection and scan as everything else first.
    Placement is a second gate, not a replacement: a file that is clean but does
    not parse, or would replace a symbol already in the library, is held with
    that reason rather than released.
    """
    if kind not in UPLOAD_KINDS:
        raise ValueError(f"kind must be one of {sorted(UPLOAD_KINDS)}, not {kind!r}")
    if kind in LIBRARY_KINDS:
        library = library_upload.check_library(library, project_dir, suggest=Path(project_dir).name)
    # Bytes that are a PDF are stored and released as one, whatever the name
    # claimed: the datasheet resolver looks for <MPN>.pdf and nothing else.
    suffix = ".pdf" if _looks_like_pdf(data) else _upload_suffix(original_name)
    rec = accept(
        data,
        project_dir,
        mpn=mpn,
        suffix=suffix,
        origin=UPLOAD,
        uploaded_by=uploaded_by,
        original_name=original_name,
        kind=kind,
        llm_ignore=llm_ignore,
        insist_on_scan=insist_on_scan,
    )
    if rec.reasons:
        return rec
    if kind == "kicad_bundle":
        return _unpack_bundle(data, project_dir, rec, library=library, insist_on_scan=insist_on_scan)
    if kind in LIBRARY_KINDS:
        return _place(data, project_dir, rec, library=library)
    if kind == "datasheet" and mpn:
        as_name = f"{_safe(mpn, 'datasheet')}{suffix}"
    else:
        as_name = f"{_safe(Path(original_name).stem, 'upload')}{suffix}"
    return release(project_dir, rec, as_name=as_name, dest_dirname=UPLOAD_KINDS[kind])


def _place(data: bytes, project_dir: str | Path, rec: Record, *, library: str) -> Record:
    """Release a passed library file into the project library, or hold it with why not."""
    project_dir = Path(project_dir)
    rec.library = library
    try:
        if rec.kind == "symbol":
            placed = library_upload.place_symbols(data, project_dir, library)
        elif rec.kind == "footprint":
            placed = library_upload.place_footprint(data, project_dir, library)
        else:
            placed = library_upload.place_model(data, project_dir, library, rec.original_name)
    except library_upload.Refused as exc:
        rec.reasons = [str(exc)]
        rec.state = HELD
        _upsert(project_dir, rec)
        return rec
    # released_to/released_as name the first file written, relative to the
    # kind's directory — a symdir symbol is <Lib>.kicad_symdir/<Name>.kicad_sym
    # under libraries/symbols — so the ledger points at what the resolver
    # reads; `placed` lists everything.
    base = UPLOAD_KINDS[rec.kind]
    rec.released_to = base
    rec.released_as = placed.paths[0][len(base) + 1:]
    rec.placed = placed.paths
    rec.names = placed.names
    rec.names_by_library = {library: list(placed.names)} if placed.names else {}
    rec.state = RELEASED
    rec.reasons = []
    if placed.note:
        rec.inspection = {**rec.inspection, "placement": placed.note}
    _upsert(project_dir, rec)
    return rec


def _unpack_bundle(data: bytes, project_dir: str | Path, rec: Record, *, library: str,
                   insist_on_scan: bool = False) -> Record:
    """Each usable entry of a passed zip through quarantine on its own, then placed.

    Every entry gets its own ledger row, its own scan and — for a PDF — its own
    inspection, exactly as if it had been uploaded alone: a clean archive says
    nothing about what a reader will do with the files inside it. Entries that
    are neither library files nor documents are not unpacked; they stay inside
    the zip, which stays in ``retrieved/``.

    ``insist_on_scan`` reaches every entry too. Dropped here, a scanner that
    answered for the zip and not for one of its entries released that entry
    unscanned in a workspace whose own environment did not require a scan.
    """
    project_dir = Path(project_dir)
    rec.library = library
    try:
        members, skipped = library_upload.unpack_bundle(data)
    except library_upload.Refused as exc:
        rec.reasons = [str(exc)]
        rec.state = HELD
        _upsert(project_dir, rec)
        return rec
    zip_stem = _safe(Path(rec.original_name).stem, "bundle")
    out: list[dict] = []
    # Models first, so a footprint from the same zip finds the model it names
    # already in the library and is linked to it as it is placed.
    order = {"3d_model": 0, "footprint": 1, "symbol": 2}
    members = sorted(members, key=lambda m: order.get(m.route, 3))
    for member in members:
        label = f"{rec.original_name}:{member.name}"
        kind = member.route
        if kind == "reference":
            # Beside the bundle's other documents rather than loose in
            # references/, so a vendor's readme.txt says whose readme it is.
            suffix = ".pdf" if _looks_like_pdf(member.data) else _upload_suffix(member.name)
            sub = accept(
                member.data, project_dir, suffix=suffix, origin=UPLOAD,
                uploaded_by=rec.uploaded_by, original_name=label, kind="reference",
                llm_ignore=rec.llm_ignore, insist_on_scan=insist_on_scan,
            )
            if not sub.reasons:
                sub = release(
                    project_dir, sub,
                    as_name=f"{_safe(Path(member.name).stem, 'file')}{suffix}",
                    dest_dirname=f"{REFERENCES_DIRNAME}/{zip_stem}",
                )
        else:
            sub = accept_upload(
                member.data, project_dir, original_name=label, kind=kind,
                uploaded_by=rec.uploaded_by, llm_ignore=rec.llm_ignore, library=library,
                insist_on_scan=insist_on_scan,
            )
        path = f"{sub.released_to}/{sub.released_as}" if sub.state == RELEASED else None
        out.append({
            "name": member.name, "kind": kind, "state": sub.state, "path": path,
            "placed": sub.placed or ([path] if path else []), "reasons": sub.reasons,
        })
    for name, reason in skipped:
        out.append({"name": name, "kind": "", "state": "skipped", "path": None, "placed": [], "reasons": [reason]})
    rec.members = out
    if not any(m["state"] == RELEASED for m in out):
        rec.reasons = [
            "nothing in it could be added"
            + (": " + "; ".join(f"{m['name']}: {', '.join(m['reasons'])}" for m in out) if out else " (it is empty)")
        ]
        rec.state = HELD
    else:
        rec.state = RELEASED
        rec.reasons = []
    _upsert(project_dir, rec)
    return rec


# ------------------------------------------------------- library files, afterwards
#
# A released datasheet or reference stays put: the ledger names it by path and
# the datasheet resolver finds it by MPN. A file placed into a project library
# is different. It is the project's library now, and a library is edited —
# a symbol replaced, a footprint dropped, a library renamed — and the
# collision rule tells people to remove what is there first. So a library
# placement may be removed or moved, and the ledger follows it: the row's
# `placed` is changed to say where its files now are, and an event saying who
# did what, and when, is appended. Events are never rewritten.

REMOVED = "removed"           # every file a library upload placed is gone
EVENT_REMOVED = "removed"
EVENT_MOVED = "moved"
EVENT_SYMBOL_REMOVED = "symbol_removed"


def events(project_dir: str | Path) -> list[dict]:
    """What happened to placed library files after they landed, oldest first."""
    return _load_events(project_dir)


def _norm(rel: str) -> str:
    return "/".join(p for p in str(rel).replace("\\", "/").split("/") if p not in ("", "."))


def _under(path: str, rel: str) -> bool:
    return path == rel or path.startswith(rel + "/")


def _is_library_row(row: dict) -> bool:
    return bool(row.get("library") or row.get("libraries"))


def library_placements_under(project_dir: str | Path, rel: str) -> list[str]:
    """Paths placed by library uploads at ``rel`` or below it."""
    rel = _norm(rel)
    return sorted({
        p for row in ledger(project_dir) if _is_library_row(row)
        for p in row.get("placed") or [] if _under(p, rel)
    })


def _sync_released(row: dict) -> None:
    """Keep released_to/released_as naming a file the row still has."""
    base = row.get("released_to") or ""
    placed = row.get("placed") or []
    if placed:
        first = placed[0]
        if base and first.startswith(base + "/"):
            row["released_as"] = first[len(base) + 1:]
        else:
            # Moved out of the kind's directory: name it from where it is now.
            row["released_to"], _, row["released_as"] = first.rpartition("/")
    else:
        row["released_as"] = ""
        if row.get("state") == RELEASED:
            row["state"] = REMOVED


def _apply(project_dir: str | Path, change, new_events: list[dict]) -> list[str]:
    """Run ``change(row)`` over every library row; save rows and append events."""
    rows = _load(project_dir)
    touched = []
    for row in rows:
        if _is_library_row(row) and change(row):
            _sync_released(row)
            touched.append(row.get("sha256", ""))
    for e in new_events:
        e["rows"] = touched
    _save(project_dir, rows, _load_events(project_dir) + new_events)
    return touched


@_transaction
def record_library_removal(project_dir: str | Path, rel: str, *, by: str) -> list[str]:
    """``rel`` (a file, or a folder of them) was deleted: the rows stop claiming it."""
    gone = set(library_placements_under(project_dir, rel))
    if not gone:
        return []

    def change(row: dict) -> bool:
        before = list(row.get("placed") or [])
        row["placed"] = [p for p in before if p not in gone]
        return row["placed"] != before

    at = _now()
    return _apply(project_dir, change, [
        {"event": EVENT_REMOVED, "path": p, "by": by, "at": at} for p in sorted(gone)
    ])


@_transaction
def record_library_move(project_dir: str | Path, src: str, dst: str, *, by: str) -> list[str]:
    """``src`` (a file or folder) was moved to ``dst``: the rows follow it."""
    src, dst = _norm(src), _norm(dst)
    moved = library_placements_under(project_dir, src)
    if not moved:
        return []
    mapping = {p: dst + p[len(src):] for p in moved}

    def change(row: dict) -> bool:
        before = list(row.get("placed") or [])
        row["placed"] = [mapping.get(p, p) for p in before]
        return row["placed"] != before

    at = _now()
    return _apply(project_dir, change, [
        {"event": EVENT_MOVED, "path": p, "to": mapping[p], "by": by, "at": at} for p in moved
    ])


@_transaction
def remove_library_symbol(project_dir: str | Path, library: str, symbol: str, *, by: str) -> dict:
    """Take one symbol out of a project library, and record it.

    Through the parser (see ``library_upload.remove_symbol``), never by cutting
    text. The upload rows that brought it stop listing it, and stop claiming
    the file once none of their symbols are left in it.
    """
    out = library_upload.remove_symbol(project_dir, library, symbol)
    before = out.pop("before")
    path = out["path"]

    def change(row: dict) -> bool:
        by_lib = _names_by_library(row)
        if symbol not in by_lib.get(library, []):
            return False
        if path not in (row.get("placed") or []):
            return False
        # Gone from this library only: the same bytes placed in another one
        # still carry the name there, and a later removal from that library
        # has to find it.
        by_lib[library] = [n for n in by_lib[library] if n != symbol]
        row["names_by_library"] = by_lib
        row["names"] = [n for n in row.get("names") or []
                        if any(n in names for names in by_lib.values())]
        mine = set(by_lib[library]) & set(out["remaining"])
        if out["file_removed"] or not mine:
            row["placed"] = [p for p in row["placed"] if p != path]
        return True

    try:
        _apply(project_dir, change, [{
            "event": EVENT_SYMBOL_REMOVED, "path": path, "library": library, "symbol": symbol,
            "file_removed": out["file_removed"], "by": by, "at": _now(),
        }])
    except BaseException:
        # The ledger still says the upload's symbol is in that file. Left
        # removed, the file would no longer be what the ledger describes, and
        # the route reports a failure it then does not commit (Codex, PR #32).
        # So the file goes back as it was.
        library_upload.restore_texts(project_dir, [(path, before)])
        raise
    return out


def ledger(project_dir: str | Path) -> list[dict]:
    """Every entry, oldest first, for anyone who wants the whole record."""
    return _load(project_dir)


def held(project_dir: str | Path) -> list[dict]:
    """Everything currently held, for anyone who wants to know what came in."""
    return [r for r in _load(project_dir) if r.get("reasons")]


def summary(project_dir: str | Path) -> dict:
    rows = _load(project_dir)
    return {
        "retrieved": len(rows),
        "released": sum(1 for r in rows if r.get("state") == RELEASED),
        "held": sum(1 for r in rows if r.get("reasons")),
        "unscanned": sum(
            1 for r in rows if (r.get("scan") or {}).get("state") == "unscanned"
        ),
    }
