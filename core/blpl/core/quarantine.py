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

import hashlib
import json
import logging
import os
import re
import shutil
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
    # The symbol or footprint names it brought, so removing one by name can
    # say which upload it came from.
    names: list[str] = field(default_factory=list)
    # A bundle's verdict per entry: name, kind, state, path, reasons.
    members: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def verdict(self) -> str:
        return verdict_of(self.to_dict())


def _load(ledger: Path) -> list[dict]:
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("retrieved", []) if isinstance(data, dict) else []


def _load_events(ledger: Path) -> list[dict]:
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    events = data.get("events", []) if isinstance(data, dict) else []
    return [e for e in events if isinstance(e, dict)]


def _save(ledger: Path, rows: list[dict], events: list[dict] | None = None) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    # Events are appended, never rewritten: a row save carries them over as
    # they are on disk.
    events = _load_events(ledger) if events is None else events
    doc: dict = {"schema_version": 1, "updated": _now(), "retrieved": rows}
    if events:
        doc["events"] = events
    body = json.dumps(doc, indent=2)
    # Write-then-move, as everywhere else here: a crash mid-write must not leave
    # a ledger that cannot be parsed, because an unreadable ledger reads as "no
    # files were ever retrieved".
    tmp = ledger.with_name(ledger.name + ".tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    tmp.replace(ledger)


def record_for(project_dir: str | Path, sha256: str) -> dict | None:
    for row in _load(quarantine_dir(project_dir) / LEDGER_NAME):
        if row.get("sha256") == sha256:
            return row
    return None


def _upsert(project_dir: str | Path, rec: Record) -> None:
    ledger = quarantine_dir(project_dir) / LEDGER_NAME
    rows = [r for r in _load(ledger) if r.get("sha256") != rec.sha256]
    rows.append(rec.to_dict())
    rows.sort(key=lambda r: (r.get("retrieved_at", ""), r.get("sha256", "")))
    _save(ledger, rows)


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


def _decide(inspection: pdf_inspect.Inspection, scan: av.ScanResult) -> list[str]:
    """Why this file may not be released. Empty means it may."""
    reasons: list[str] = []

    if scan.state == "infected":
        reasons.append(f"a malware scanner matched {scan.signature or 'a signature'}")
    if scan.state in ("unscanned", "error") and require_scan():
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
) -> Record:
    """Take delivery of a retrieved file: store, inspect, scan, decide.

    Returns the record with its verdict. The bytes are written into
    ``retrieved/`` before anything else happens, so a file that crashes the
    inspector is still on disk to look at afterwards.

    An ``UPLOAD`` of a type no inspector here understands is recorded as
    ``not_inspected`` and may be released; a distributor fetch of one is held.
    """
    project_dir = Path(project_dir)
    qdir = quarantine_dir(project_dir)
    qdir.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256(data).hexdigest()
    # Content-addressed, with the part number kept for a human reading the
    # directory. The hash leads so two files for one MPN cannot collide and so
    # nothing here can be mistaken for a finished, trusted artifact.
    label = mpn or Path(original_name).stem
    name = f"{digest[:12]}-{_safe(label, 'retrieved')}{suffix}"
    blob = qdir / name
    if not blob.exists():
        tmp = blob.with_name(blob.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(blob)

    if suffix.lower() == ".pdf" or _looks_like_pdf(data):
        inspection = pdf_inspect.inspect(blob)
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
    reasons = _decide(inspection, scan)

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
    dest_dir = project_dir / dest_dirname
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (as_name or f"{_safe(rec.mpn, 'datasheet')}.pdf")
    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() != rec.sha256:
        dest = dest.with_name(f"{dest.stem}-{rec.sha256[:8]}{dest.suffix}")
    shutil.copy2(quarantine_dir(project_dir) / rec.filename, dest)

    rec.state = RELEASED
    rec.released_as = dest.name
    rec.released_to = dest_dirname
    _upsert(project_dir, rec)
    return rec


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


def _upload_suffix(original_name: str) -> str:
    """The extension the uploader's filename claims, made safe to put on disk."""
    suffix = Path(original_name or "").suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9_]{1,15}", suffix) else ""


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
    )
    if rec.reasons:
        return rec
    if kind == "kicad_bundle":
        return _unpack_bundle(data, project_dir, rec, library=library)
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
    rec.state = RELEASED
    rec.reasons = []
    if placed.note:
        rec.inspection = {**rec.inspection, "placement": placed.note}
    _upsert(project_dir, rec)
    return rec


def _unpack_bundle(data: bytes, project_dir: str | Path, rec: Record, *, library: str) -> Record:
    """Each usable entry of a passed zip through quarantine on its own, then placed.

    Every entry gets its own ledger row, its own scan and — for a PDF — its own
    inspection, exactly as if it had been uploaded alone: a clean archive says
    nothing about what a reader will do with the files inside it. Entries that
    are neither library files nor documents are not unpacked; they stay inside
    the zip, which stays in ``retrieved/``.
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
                llm_ignore=rec.llm_ignore,
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
    return _load_events(quarantine_dir(project_dir) / LEDGER_NAME)


def _norm(rel: str) -> str:
    return "/".join(p for p in str(rel).replace("\\", "/").split("/") if p not in ("", "."))


def _under(path: str, rel: str) -> bool:
    return path == rel or path.startswith(rel + "/")


def library_placements_under(project_dir: str | Path, rel: str) -> list[str]:
    """Paths placed by library uploads at ``rel`` or below it."""
    rel = _norm(rel)
    return sorted({
        p for row in ledger(project_dir) if row.get("library")
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
    path = quarantine_dir(project_dir) / LEDGER_NAME
    rows = _load(path)
    touched = []
    for row in rows:
        if row.get("library") and change(row):
            _sync_released(row)
            touched.append(row.get("sha256", ""))
    for e in new_events:
        e["rows"] = touched
    _save(path, rows, _load_events(path) + new_events)
    return touched


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


def remove_library_symbol(project_dir: str | Path, library: str, symbol: str, *, by: str) -> dict:
    """Take one symbol out of a project library, and record it.

    Through the parser (see ``library_upload.remove_symbol``), never by cutting
    text. The upload rows that brought it stop listing it, and stop claiming
    the file once none of their symbols are left in it.
    """
    out = library_upload.remove_symbol(project_dir, library, symbol)
    path = out["path"]

    def change(row: dict) -> bool:
        if row.get("library") != library or symbol not in (row.get("names") or []):
            return False
        if path not in (row.get("placed") or []):
            return False
        row["names"] = [n for n in row["names"] if n != symbol]
        mine = set(row["names"]) & set(out["remaining"])
        if out["file_removed"] or not mine:
            row["placed"] = [p for p in row["placed"] if p != path]
        return True

    _apply(project_dir, change, [{
        "event": EVENT_SYMBOL_REMOVED, "path": path, "library": library, "symbol": symbol,
        "file_removed": out["file_removed"], "by": by, "at": _now(),
    }])
    return out


def ledger(project_dir: str | Path) -> list[dict]:
    """Every entry, oldest first, for anyone who wants the whole record."""
    return _load(quarantine_dir(project_dir) / LEDGER_NAME)


def held(project_dir: str | Path) -> list[dict]:
    """Everything currently held, for anyone who wants to know what came in."""
    return [r for r in _load(quarantine_dir(project_dir) / LEDGER_NAME) if r.get("reasons")]


def summary(project_dir: str | Path) -> dict:
    rows = _load(quarantine_dir(project_dir) / LEDGER_NAME)
    return {
        "retrieved": len(rows),
        "released": sum(1 for r in rows if r.get("state") == RELEASED),
        "held": sum(1 for r in rows if r.get("reasons")),
        "unscanned": sum(
            1 for r in rows if (r.get("scan") or {}).get("state") == "unscanned"
        ),
    }
