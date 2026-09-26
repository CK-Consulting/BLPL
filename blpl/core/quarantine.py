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
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import av, pdf_inspect

QUARANTINE_DIRNAME = "retrieved"
TRUSTED_DIRNAME = "datasheets"
REFERENCES_DIRNAME = "references"
LEDGER_NAME = "quarantine.json"

# Where an upload of each kind lands once it passes. The uploader picks the
# kind, so the directory name keeps saying what is in it: a datasheet goes where
# the extraction tools already look, and anything else goes beside it.
UPLOAD_KINDS = {"datasheet": TRUSTED_DIRNAME, "reference": REFERENCES_DIRNAME}

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

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


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

    def to_dict(self) -> dict:
        return asdict(self)


def _load(ledger: Path) -> list[dict]:
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("retrieved", []) if isinstance(data, dict) else []


def _save(ledger: Path, rows: list[dict]) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {"schema_version": 1, "updated": _now(), "retrieved": rows}, indent=2
    )
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


def _looks_like_pdf(data: bytes) -> bool:
    # The spec allows junk before the header; readers look within the first 1 KB.
    return b"%PDF-" in data[:1024]


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
    )
    _upsert(project_dir, rec)
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
) -> Record:
    """The whole path for a fetched datasheet, in the order it has to happen."""
    rec = accept(
        data, project_dir, mpn=mpn, distributor=distributor, source_url=source_url
    )
    if not rec.reasons:
        rec = release(project_dir, rec, as_name=as_name)
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
) -> Record:
    """The whole path for a file a member uploads, in the order it has to happen.

    Released into the directory for its ``kind``: a datasheet with an MPN is
    named after the part, the way a fetched one is, so the resolver finds it;
    anything else keeps the uploader's filename, made safe.
    """
    if kind not in UPLOAD_KINDS:
        raise ValueError(f"kind must be one of {sorted(UPLOAD_KINDS)}, not {kind!r}")
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
    if kind == "datasheet" and mpn:
        as_name = f"{_safe(mpn, 'datasheet')}{suffix}"
    else:
        as_name = f"{_safe(Path(original_name).stem, 'upload')}{suffix}"
    return release(project_dir, rec, as_name=as_name, dest_dirname=UPLOAD_KINDS[kind])


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
