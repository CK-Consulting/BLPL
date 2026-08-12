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

from dataclasses import dataclass, field
from pathlib import Path

from ..kicad_happy import DISTRIBUTORS, CredResolver, run_script, script_path


@dataclass
class PartHit:
    distributor: str
    mpn: str = ""
    manufacturer: str = ""
    description: str = ""
    datasheet_url: str = ""

    def to_dict(self) -> dict:
        return {
            "distributor": self.distributor,
            "mpn": self.mpn,
            "manufacturer": self.manufacturer,
            "description": self.description,
            "datasheet_url": self.datasheet_url,
        }


@dataclass
class PartSearch:
    query: str
    hits: list[PartHit] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)   # distributor → why
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
) -> PartSearch:
    """Resolve an MPN across distributors. Never raises for a missing part."""
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
            res = run_script(
                path,
                ["--search", mpn, "--json", "-o", "/dev/null"],
                env=creds.env_for(dist),
                timeout=timeout,
            )
        except Exception as exc:  # subprocess/timeout
            result.skipped[dist] = f"{type(exc).__name__}: {exc}"
            continue

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

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "mpn": self.mpn,
            "path": self.path,
            "distributor": self.distributor,
            # kicad-happy re-reads the downloaded PDF and checks the MPN really
            # appears in it. A "wrong" here means we fetched somebody else's
            # datasheet — worth surfacing rather than filing quietly.
            "verification": self.verification,
            "detail": self.detail,
        }


def fetch_datasheet(
    mpn: str,
    dest_dir: Path,
    *,
    creds: CredResolver | None = None,
    distributors: list[str] | None = None,
    timeout: int = 180,
) -> DatasheetFetch:
    """Download one datasheet into ``dest_dir``, trying distributors in order."""
    creds = creds or CredResolver()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in mpn)
    out = dest_dir / f"{safe}.pdf"
    if out.is_file() and out.stat().st_size > 0:
        return DatasheetFetch(
            ok=True, mpn=mpn, path=str(out), distributor="cache", detail="already downloaded"
        )

    tried: list[str] = []
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
        res = run_script(
            path,
            ["--search", mpn, "--json", "-o", str(out)],
            env=creds.env_for(dist),
            timeout=timeout,
        )
        data = res.data if isinstance(res.data, dict) else {}
        if res.ok and out.is_file() and out.stat().st_size > 0:
            ver = (data.get("verification") or {}) if isinstance(data, dict) else {}
            return DatasheetFetch(
                ok=True,
                mpn=mpn,
                path=str(out),
                distributor=dist,
                verification=str(ver.get("confidence") or "unverified"),
                detail=str(ver.get("details") or ""),
            )

    return DatasheetFetch(
        ok=False,
        mpn=mpn,
        detail=(
            f"no datasheet found via {', '.join(tried)}"
            if tried
            else "no distributor is configured — add a key in Settings"
        ),
    )
