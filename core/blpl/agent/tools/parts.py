"""Parts lookup and datasheet retrieval across the distributors we have keys for.

kicad-happy documents structured part search as ``curl`` recipes in each skill's
SKILL.md — guidance for an agent with a shell, not something a server can call.
What it *does* ship is a uniform per-distributor script that resolves an MPN to
a manufacturer, description, and datasheet URL. This fans that out across every
configured distributor and merges the answers.

Fanning out rather than picking one is the point. A part that DigiKey has never
heard of is often stocked by LCSC; agreement between two distributors on the
manufacturer is real evidence, and disagreement is worth seeing rather than
averaging away. Distributors we have no key for are reported as named skips, so
"nothing found" is never confused with "nobody looked".
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ...core import quarantine
from ..kicad_happy import DISTRIBUTORS, CredResolver, run_script, script_path


@dataclass
class PartHit:
    distributor: str
    mpn: str = ""
    manufacturer: str = ""
    description: str = ""
    datasheet_url: str = ""
    # Parametric data as the distributor states it — voltage rating, tolerance,
    # dielectric, power (Mouser as ProductAttributes, DigiKey as Parameters),
    # and since kicad-happy PR #1 what it says about the package: `package`,
    # `supplier_package`, `body_mm`, `height_mm`.
    #
    # The package half is a *hint*, not a land pattern. "24-WQFN (4x4)" narrows a
    # footprint search and does not settle it: no distributor states the exposed
    # pad, which is the dimension that tells a dozen no-lead footprints apart,
    # and that comes only from the datasheet's package drawing.
    attributes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {
            "distributor": self.distributor,
            "mpn": self.mpn,
            "manufacturer": self.manufacturer,
            "description": self.description,
            "datasheet_url": self.datasheet_url,
        }
        if self.attributes:
            out["attributes"] = dict(self.attributes)
        return out


@dataclass
class PartSearch:
    query: str
    hits: list[PartHit] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)   # distributor → why
    # What the lookup brought back with it, and what quarantine made of it.
    retrieved: list[dict] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "hits": [h.to_dict() for h in self.hits],
            # Both of these matter to a reader deciding whether to trust an
            # empty result: skipped means nobody asked, not_found means asked
            # and told no.
            "skipped": self.skipped,
            "not_found": self.not_found,
            "retrieved": self.retrieved,
            "agreement": self.agreement(),
        }

    def agreement(self) -> dict:
        """Where the distributors agree, and where they don't."""
        mfrs = {h.manufacturer.strip() for h in self.hits if h.manufacturer.strip()}
        sheets = {h.datasheet_url for h in self.hits if h.datasheet_url}
        return {
            "manufacturers": sorted(mfrs),
            "manufacturer_conflict": len(mfrs) > 1,
            "datasheet_urls": len(sheets),
        }


def search_parts(
    mpn: str,
    *,
    creds: CredResolver | None = None,
    distributors: list[str] | None = None,
    timeout: int = 60,
    project_dir: Path | None = None,
) -> PartSearch:
    """Resolve an MPN across distributors. Never raises for a missing part.

    The resolver scripts download the datasheet as part of answering, and for a
    long time this pointed them at ``/dev/null`` — paying for the download,
    including the scrape and browser fallbacks, and discarding the result. Given
    ``project_dir`` the file is kept instead, through ``quarantine``: the bytes
    land in ``retrieved/``, get inspected and scanned, and only reach
    ``datasheets/`` if they pass. Keeping a file we already fetched costs
    nothing; fetching it twice costs a minute and a page-scrape each time.
    """
    creds = creds or CredResolver()
    result = PartSearch(query=mpn)

    for dist in distributors or list(DISTRIBUTORS):
        missing = creds.missing_for(dist)
        if missing:
            result.skipped[dist] = f"not configured — missing {', '.join(missing)}"
            continue
        try:
            path = script_path(dist, f"fetch_datasheet_{dist}.py")
        except Exception as exc:  # kicad-happy absent
            result.skipped[dist] = str(exc)
            continue
        if not path.exists():
            result.skipped[dist] = f"{path.name} not present in this kicad-happy checkout"
            continue

        try:
            # --search resolves the part without downloading the PDF; the
            # download is a separate, explicitly-approved step.
            with _download_target(project_dir) as target:
                res = run_script(
                    path,
                    ["--search", mpn, "--json", "-o", str(target)],
                    env=creds.env_for(dist),
                    timeout=timeout,
                )
                kept = _keep(target, project_dir, mpn=mpn, distributor=dist)
        except Exception as exc:  # subprocess/timeout
            result.skipped[dist] = f"{type(exc).__name__}: {exc}"
            continue
        if kept is not None:
            result.retrieved.append(kept)

        data = res.data if isinstance(res.data, dict) else None
        if not data or not (data.get("mpn") or data.get("datasheet_url")):
            result.not_found.append(dist)
            continue
        result.hits.append(
            PartHit(
                distributor=dist,
                mpn=str(data.get("mpn") or mpn),
                manufacturer=str(data.get("manufacturer") or ""),
                description=str(data.get("description") or ""),
                datasheet_url=str(data.get("datasheet_url") or ""),
                attributes=dict(data.get("attributes") or {}),
            )
        )
    return result


@dataclass
class DatasheetFetch:
    ok: bool
    mpn: str
    path: str = ""
    distributor: str = ""
    verification: str = ""
    detail: str = ""
    # Where a person can go and get it by hand. The distributor knows the part
    # and shows the datasheet on the product page; it is only the automated
    # download that was blocked, so a failure that names the page is a minute's
    # work and one that does not is a dead end.
    manual_urls: dict[str, str] = field(default_factory=dict)
    # What quarantine made of the file that was released, and of any that were
    # not. A caller that wants to know *why* it has no datasheet should not have
    # to go and read a ledger to find out.
    quarantine: dict = field(default_factory=dict)
    held: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "mpn": self.mpn,
            "path": self.path,
            "distributor": self.distributor,
            "manual_urls": dict(self.manual_urls),
            "quarantine": dict(self.quarantine),
            "held": list(self.held),
            # kicad-happy re-reads the downloaded PDF and checks the MPN really
            # appears in it. A "wrong" here means we fetched somebody else's
            # datasheet — worth surfacing rather than filing quietly.
            "verification": self.verification,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Taking delivery
#
# Every path that downloads a file routes through here, so there is exactly one
# place where retrieved bytes become project bytes — and it is the place that
# inspects and scans them first.
# ---------------------------------------------------------------------------


@contextmanager
def _download_target(project_dir: Path | None):
    """Somewhere for a resolver script to write, that is not the project.

    Even the temporary file is kept out of ``datasheets/``: a partial download
    sitting under the name that means "checked" — even for a second, even on a
    crash — is the state this whole arrangement exists to prevent.
    """
    if project_dir is None:
        # Nowhere to keep it, so the old behaviour: let the script write to the
        # bit bucket. Returned before the try/finally below on purpose — the
        # cleanup there must never be pointed at /dev/null.
        yield Path(os.devnull)
        return
    staging = quarantine.quarantine_dir(project_dir) / ".incoming"
    staging.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".pdf", dir=staging)
    os.close(fd)
    target = Path(name)
    try:
        yield target
    finally:
        target.unlink(missing_ok=True)


def _keep(
    target: Path,
    project_dir: Path | None,
    *,
    mpn: str,
    distributor: str,
    source_url: str = "",
    dest: Path | None = None,
) -> dict | None:
    """Hand a downloaded file to quarantine, if there is one to hand it to.

    ``dest`` is where a passed file is released to — the exact path the caller
    will look for next time, so "already downloaded" can find it.
    """
    if project_dir is None or not target.is_file() or target.stat().st_size == 0:
        return None
    try:
        data = target.read_bytes()
    except OSError:
        return None
    where: dict = {}
    if dest is not None:
        where = {
            "as_name": dest.name,
            "dest_dirname": dest.parent.relative_to(project_dir).as_posix(),
        }
    rec = quarantine.accept_and_release(
        data, project_dir, mpn=mpn, distributor=distributor, source_url=source_url, **where,
    )
    return rec.to_dict()


def fetch_datasheet(
    mpn: str,
    dest_dir: Path,
    *,
    creds: CredResolver | None = None,
    distributors: list[str] | None = None,
    timeout: int = 180,
    project_dir: Path | None = None,
) -> DatasheetFetch:
    """Download one datasheet, trying distributors in order.

    The file does not arrive in ``dest_dir``. It arrives in ``retrieved/``,
    where it is inspected for things a document has no business doing and — if a
    scanner is configured — scanned, and it reaches ``dest_dir`` only if it
    passes. A file that does not pass is kept where it landed and reported, not
    deleted: which part it was for and what was in it is worth knowing, and
    throwing it away only means downloading it again tomorrow.
    """
    creds = creds or CredResolver()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Where retrieved/ lives. Given, not guessed: this used to be inferred as
    # the parent of dest_dir, which held only while datasheets landed at the
    # top of datasheets/. Once the agent filed each one under its part
    # (datasheets/<MPN>/), the "project" became datasheets/ itself — so the
    # quarantine was created at datasheets/retrieved/, outside the ignore rule
    # that keeps uncleared downloads out of git, its held files became
    # candidates for every datasheet lookup, and released files landed in
    # datasheets/datasheets/ where "already downloaded" never looked.
    # The parent is kept only as the fallback for callers that write to the
    # top level, where it is still right.
    project_dir = Path(project_dir) if project_dir is not None else dest_dir.parent
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in mpn)
    out = dest_dir / f"{safe}.pdf"
    if out.is_file() and out.stat().st_size > 0:
        return DatasheetFetch(
            ok=True, mpn=mpn, path=str(out), distributor="cache", detail="already downloaded"
        )

    tried: list[str] = []
    reasons: list[str] = []
    manual: dict[str, str] = {}
    held: list[dict] = []
    for dist in distributors or list(DISTRIBUTORS):
        if creds.missing_for(dist):
            continue
        try:
            path = script_path(dist, f"fetch_datasheet_{dist}.py")
        except Exception:
            break
        if not path.exists():
            continue
        tried.append(dist)
        with _download_target(project_dir) as target:
            res = run_script(
                path,
                ["--search", mpn, "--json", "-o", str(target)],
                env=creds.env_for(dist),
                timeout=timeout,
            )
            data = res.data if isinstance(res.data, dict) else {}
            record = _keep(
                target, project_dir, mpn=mpn, distributor=dist,
                source_url=str(data.get("datasheet_url") or ""),
                dest=out,
            )
        if url := str(data.get("manual_url") or ""):
            manual[dist] = url
        if reason := str(data.get("error") or "").strip():
            reasons.append(f"{dist}: {reason}")
        elif not res.ok and not data:
            # No JSON at all means it did not get far enough to explain itself.
            reasons.append(f"{dist}: exited {res.exit_code} without a result")
        if record is not None and record.get("state") == quarantine.RELEASED:
            ver = (data.get("verification") or {}) if isinstance(data, dict) else {}
            return DatasheetFetch(
                ok=True,
                mpn=mpn,
                path=str(project_dir / record["released_to"] / record["released_as"]),
                distributor=dist,
                verification=str(ver.get("confidence") or "unverified"),
                detail=str(ver.get("details") or ""),
                quarantine=record,
            )
        if record is not None:
            # Downloaded and held. Not a failure to find the datasheet — a
            # refusal to hand this one over, which is a different sentence and
            # needs to read like one.
            held.append(record)
            reasons.append(f"{dist}: held in quarantine — " + "; ".join(record["reasons"]))

    if not tried:
        return DatasheetFetch(
            ok=False, mpn=mpn,
            detail="no distributor is configured — add a key in Settings",
        )
    # Say what happened, not just that nothing happened. "no datasheet found"
    # reads as "this part has no datasheet", which is almost never true — the
    # usual cause is a distributor blocking the download, and that is a
    # different problem with a different fix.
    detail = f"no datasheet downloaded for {mpn} — tried {', '.join(tried)}"
    if reasons:
        detail += "; " + "; ".join(reasons)
    if manual:
        detail += ". Reachable by hand at: " + ", ".join(
            f"{d} {u}" for d, u in sorted(manual.items())
        )
    if held:
        detail += (
            f". {len(held)} file(s) were downloaded and are being held in "
            f"{quarantine.QUARANTINE_DIRNAME}/ — see {quarantine.LEDGER_NAME} for what was found"
        )
    return DatasheetFetch(
        ok=False, mpn=mpn, detail=detail, manual_urls=manual, held=held
    )
