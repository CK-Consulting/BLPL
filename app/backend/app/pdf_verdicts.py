"""Whether the browser may render a PDF, decided by its bytes and not its path.

The blob route used to serve every ``.pdf`` outside ``retrieved/`` as
``application/pdf``, inline, so the browser's own viewer opened it. The
directory was meant to say whether a file had passed quarantine, but nothing
made a file in the checkout prove that. PDFs reach a checkout by doors that
never go near quarantine: a commit pulled from a remote, a project import, a
library part staged into ``datasheets/``, a pipeline stage, the KiCad desktop.
Any of them could put a PDF with an ``/OpenAction`` where the browser would
open it.

So trust follows the content hash. ``models.PdfVerdict`` holds what this
backend's own inspection found for exact bytes, per project, and a PDF is
rendered inline only when the bytes about to be sent hash to a row that says
``clean`` and names the current inspector. Everything else is an attachment.

**Where a verdict comes from.** Only from an assessment this backend ran:
``accept_upload`` at upload, ``assess`` at chat attach, or ``assess`` when a
member asks for one. The in-tree ledger is committed project content, so a
pull can carry a row claiming any hash was released; it is read only for rows
that say *held*, which can keep a file out of the viewer and never let one in.

**Why a member has to ask, rather than the first view inspecting.** A GET
happens on any link, pasted URL or prefetch, and inspection is the step that
turns unknown bytes into ones the browser will render. That is a state change
made on someone's behalf, so it is a POST, from a button, recorded with who
pressed it. It costs one click per distinct file per project, once.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from blpl.core import quarantine

from .models import PdfVerdict

UPLOAD = "upload"
ATTACHMENT = "attachment"
INSPECT = "inspect"
LEDGER = "ledger"


def renders_inline(row: PdfVerdict | None) -> bool:
    """Whether bytes with this verdict may go to the browser's PDF viewer.

    Clean and from the inspector running now. ``unscanned`` is not enough:
    quarantine may release a file no scanner looked at, but rendering it is a
    stronger claim than keeping it, and the viewer is what an exploit targets.
    """
    return (
        row is not None
        and row.verdict == quarantine.CLEAN
        and row.inspector == quarantine.INSPECTOR
    )


def find(session: Session, project_id: int, sha256: str) -> PdfVerdict | None:
    return session.get(PdfVerdict, (project_id, sha256))


def record(
    session: Session,
    project_id: int,
    sha256: str,
    *,
    verdict: str,
    inspector: str,
    inspection_state: str,
    scanner: str,
    scan_state: str,
    reasons: list[str],
    source: str,
    user_id: int | None,
) -> PdfVerdict:
    """Store this backend's latest finding for these bytes, replacing any older one.

    Newest wins, including a clean after a held: both came from running the
    checks on the same bytes, so the later run reflects the current inspector
    and scanner, which is the question being asked.
    """
    fields = dict(
        verdict=verdict,
        inspector=inspector,
        inspection_state=inspection_state,
        scanner=scanner,
        scan_state=scan_state,
        reasons=list(reasons),
        source=source,
        decided_by=user_id,
        decided_at=datetime.now(timezone.utc),
    )
    row = find(session, project_id, sha256)
    if row is None:
        row = PdfVerdict(project_id=project_id, sha256=sha256, **fields)
        try:
            # A savepoint, so two first views racing to insert the same key
            # cost one of them an update rather than the whole request.
            with session.begin_nested():
                session.add(row)
        except IntegrityError:
            row = find(session, project_id, sha256)
            assert row is not None
            for key, value in fields.items():
                setattr(row, key, value)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    session.flush()
    return row


def record_assessment(
    session: Session,
    project_id: int,
    sha256: str,
    assessment: quarantine.Assessment,
    *,
    source: str,
    user_id: int | None,
) -> PdfVerdict:
    return record(
        session,
        project_id,
        sha256,
        verdict=assessment.verdict,
        inspector=assessment.inspector,
        inspection_state=assessment.inspection.state,
        scanner=assessment.scan.scanner,
        scan_state=assessment.scan.state,
        reasons=assessment.reasons,
        source=source,
        user_id=user_id,
    )


def record_ledger_row(
    session: Session, project_id: int, row: dict, *, source: str, user_id: int | None
) -> PdfVerdict:
    """A verdict from a ledger row this backend has just written itself."""
    return record(
        session,
        project_id,
        row["sha256"],
        verdict=quarantine.verdict_of(row),
        inspector=row.get("inspector", ""),
        inspection_state=(row.get("inspection") or {}).get("state", ""),
        scanner=(row.get("scan") or {}).get("scanner", ""),
        scan_state=(row.get("scan") or {}).get("state", ""),
        reasons=list(row.get("reasons") or []),
        source=source,
        user_id=user_id,
    )


def lookup(session: Session, project_id: int, sha256: str, project_dir) -> PdfVerdict | None:
    """The verdict for these bytes, importing a *held* ledger row if there is none.

    A released ledger row is not imported. The ledger is committed, so a pull
    could bring one in for any hash, and accepting it would be the path-based
    trust this replaces with one more step. A held row can only keep the
    browser from rendering a file, so taking it on the ledger's word is safe,
    and it means the reasons are on screen before anyone downloads the file.
    """
    row = find(session, project_id, sha256)
    if row is not None:
        return row
    entry = quarantine.record_for(project_dir, sha256)
    if entry is None or quarantine.verdict_of(entry) != quarantine.HELD:
        return None
    return record_ledger_row(session, project_id, entry, source=LEDGER, user_id=None)


def describe(row: PdfVerdict | None) -> dict:
    """The verdict as the client shows it. ``None`` means never inspected."""
    if row is None:
        return {
            "verdict": None,
            "inline": False,
            "reasons": [],
            "inspector": "",
            "current_inspector": quarantine.INSPECTOR,
            "scan": "",
            "scanner": "",
            "source": "",
            "decided_at": None,
        }
    return {
        "verdict": row.verdict,
        "inline": renders_inline(row),
        "reasons": list(row.reasons or []),
        "inspector": row.inspector,
        "current_inspector": quarantine.INSPECTOR,
        "scan": row.scan_state,
        "scanner": row.scanner,
        "source": row.source,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }

