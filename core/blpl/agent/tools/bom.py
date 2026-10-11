"""Turning BLPL's BOM into files an assembly house will actually accept.

`release.py` already writes a grouped BOM CSV and a kicad-cli placement file.
Neither is uploadable: JLCPCB and PCBWay each want their own columns, in their
own order, with their own idea of what a missing field means. kicad-happy's
``bom`` skill ships the JLCPCB translator; this drives it, and fills the two
gaps it deliberately leaves.

**The LCSC column.** ``translate_bom_pnp.py`` writes ``LCSC Part #`` as an empty
column and leaves it to the user. On a JLCPCB *assembly* order that column is
the order — JLCPCB sources from LCSC by C-number, not by MPN, so a BOM without
it is a BOM they cannot build. Filling it needs a lookup per part, so it is
opt-in; and because a part can resolve to a C-number that has no stock, the fill
reports stock rather than just presence. A confidently-wrong C-number is worse
than a blank one.

**PCBWay's BOM.** The pcbway skill is documentation only — no scripts — and its
columns (``Line#, Qty, Designator, MPN, Manufacturer, Description, Package,
Type``) are not JLCPCB's. So that writer lives here. PCBWay sources turnkey by
MPN, which makes MPN the field that matters and LCSC numbers irrelevant; the two
houses genuinely need different files, and emitting one and calling it both
would produce a quote for the wrong parts.

``Type`` (SMD/THT) is read from the footprint library rather than guessed from
the footprint name, and left blank when the library has no answer. A wrong
mounting type is a re-quote at best and a hand-assembly surprise at worst.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..kicad_happy import CredResolver, KicadHappyMissing, run_script, script_path

HOUSES = ("jlcpcb", "pcbway")

# PCBWay's turnkey BOM, per skills/pcbway/SKILL.md. Order matters to them.
PCBWAY_COLUMNS = (
    "Line#",
    "Qty",
    "Designator",
    "MPN",
    "Manufacturer",
    "Description",
    "Package",
    "Type",
)


@dataclass
class BomRow:
    """One grouped line item, as release.py's CSV holds it."""

    designators: list[str] = field(default_factory=list)
    quantity: int = 0
    value: str = ""
    footprint: str = ""
    mpn: str = ""
    manufacturer: str = ""

    @property
    def package(self) -> str:
        """The footprint's own name, without its library prefix.

        ``Capacitor_SMD:C_0402_1005Metric`` → ``C_0402_1005Metric``. Both houses
        want the package, not the KiCad library path it happened to come from.
        """
        return self.footprint.split(":", 1)[-1] if self.footprint else ""


def read_bom_csv(path: Path) -> list[BomRow]:
    """Read the grouped BOM release.py writes."""
    rows: list[BomRow] = []
    with Path(path).open(newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            desig = [d.strip() for d in (raw.get("Designators") or "").split(",") if d.strip()]
            try:
                qty = int(raw.get("Quantity") or 0)
            except ValueError:
                qty = len(desig)
            rows.append(
                BomRow(
                    designators=desig,
                    quantity=qty or len(desig),
                    value=(raw.get("Value") or "").strip(),
                    footprint=(raw.get("Footprint") or "").strip(),
                    mpn=(raw.get("MPN") or "").strip(),
                    manufacturer=(raw.get("Manufacturer") or "").strip(),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Mounting type, from the library rather than the name
# ---------------------------------------------------------------------------

_ATTR = re.compile(r"\(attr\s+([^)]*)\)")


def mount_type(footprint: str, roots: list[Path]) -> str:
    """``SMD``, ``THT``, or "" when the library cannot say.

    Read from the ``.kicad_mod``'s own ``(attr ...)`` rather than inferred from
    the footprint name. Names lie — ``Capacitor_SMD:...`` is a reliable-looking
    prefix right up to the through-hole part somebody filed under it — and this
    field decides whether a house quotes reflow or hand assembly.
    """
    if ":" not in footprint:
        return ""
    lib, name = footprint.split(":", 1)
    for root in roots:
        mod = Path(root) / f"{lib}.pretty" / f"{name}.kicad_mod"
        if not mod.is_file():
            continue
        try:
            text = mod.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = _ATTR.search(text)
        attrs = found.group(1).lower() if found else ""
        if "smd" in attrs:
            return "SMD"
        if "through_hole" in attrs:
            return "THT"
        # A footprint with no attr at all is KiCad's default, which is
        # through-hole for legacy libraries — but "probably" is not good enough
        # for a field a quote depends on.
        return ""
    return ""


# ---------------------------------------------------------------------------
# LCSC part numbers — the column JLCPCB actually orders from
# ---------------------------------------------------------------------------


@dataclass
class LcscPart:
    mpn: str
    lcsc: str = ""
    stock: int = 0
    manufacturer: str = ""
    description: str = ""

    @property
    def orderable(self) -> bool:
        return bool(self.lcsc) and self.stock > 0

    def to_dict(self) -> dict:
        return {
            "mpn": self.mpn,
            "lcsc": self.lcsc,
            "stock": self.stock,
            "manufacturer": self.manufacturer,
            "description": self.description,
            "orderable": self.orderable,
        }


def resolve_lcsc(
    mpns: list[str], *, creds: CredResolver | None = None, timeout: int = 60
) -> dict[str, LcscPart]:
    """Map each MPN to its LCSC part number and stock.

    LCSC needs no credentials — it reads a public endpoint — which is why this
    is the one distributor lookup that always works. An MPN that resolves to
    nothing is simply absent from the result; the caller reports it rather than
    substituting a guess.
    """
    creds = creds or CredResolver()
    found: dict[str, LcscPart] = {}
    try:
        path = script_path("lcsc", "fetch_datasheet_lcsc.py")
    except KicadHappyMissing:
        return found
    if not path.is_file():
        return found

    for mpn in dict.fromkeys(m for m in mpns if m):
        try:
            res = run_script(
                path,
                ["--search", mpn, "--json", "-o", "/dev/null"],
                env=creds.env_for("lcsc"),
                timeout=timeout,
            )
        except Exception:
            continue
        data = res.data if isinstance(res.data, dict) else None
        if not data or not data.get("lcsc"):
            continue
        try:
            stock = int(data.get("in_stock") or 0)
        except (TypeError, ValueError):
            stock = 0
        found[mpn] = LcscPart(
            mpn=mpn,
            lcsc=str(data["lcsc"]),
            stock=stock,
            manufacturer=str(data.get("manufacturer") or ""),
            description=str(data.get("description") or ""),
        )
    return found


def fill_lcsc_column(bom_csv: Path, parts: dict[str, LcscPart]) -> tuple[int, list[str]]:
    """Write resolved C-numbers into a JLCPCB BOM in place.

    Returns ``(filled, unbuildable)`` where *unbuildable* names the designators
    still left without a C-number. Those are the lines JLCPCB cannot assemble —
    reported by designator rather than counted, because the answer to "which
    ones" is what the user has to act on.
    """
    bom_csv = Path(bom_csv)
    with bom_csv.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        return 0, []
    header = rows[0]
    try:
        col_lcsc = header.index("LCSC Part #")
        col_mpn = header.index("MPN")
        col_des = header.index("Designator")
    except ValueError:
        return 0, []

    filled = 0
    unbuildable: list[str] = []
    for row in rows[1:]:
        if len(row) <= max(col_lcsc, col_mpn, col_des):
            continue
        part = parts.get(row[col_mpn].strip())
        if part and part.lcsc:
            row[col_lcsc] = part.lcsc
            filled += 1
        elif not row[col_lcsc].strip():
            unbuildable.extend(d.strip() for d in row[col_des].split(",") if d.strip())
    with bom_csv.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return filled, unbuildable


# ---------------------------------------------------------------------------
# The house formats
# ---------------------------------------------------------------------------


@dataclass
class FabFile:
    name: str
    path: str
    rows: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "rows": self.rows, "note": self.note}


@dataclass
class AssemblyPackage:
    ok: bool
    house: str
    files: list[FabFile] = field(default_factory=list)
    # Things that will cost a round-trip with the fab if nobody looks. Never
    # fatal — a package that refuses to build teaches less than one that builds
    # and says what is wrong with it.
    warnings: list[str] = field(default_factory=list)
    lcsc: list[dict] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "house": self.house,
            "files": [f.to_dict() for f in self.files],
            "warnings": self.warnings,
            "lcsc": self.lcsc,
            "reason": self.reason,
        }


def _translate(subcommand: str, args: list[str], timeout: int = 120):
    path = script_path("bom", "translate_bom_pnp.py")
    if not path.is_file():
        raise KicadHappyMissing("this kicad-happy checkout has no translate_bom_pnp.py")
    return run_script(path, [subcommand, *args], timeout=timeout)


def _row_count(path: Path) -> int:
    try:
        with Path(path).open(newline="", encoding="utf-8") as f:
            return max(0, sum(1 for _ in csv.reader(f)) - 1)
    except OSError:
        return 0


def write_pcbway_bom(rows: list[BomRow], out_csv: Path, *, footprint_roots: list[Path]) -> FabFile:
    """PCBWay's turnkey BOM. Ours to write — the skill ships no script."""
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    no_mpn: list[str] = []
    no_type = 0
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(PCBWAY_COLUMNS)
        for i, row in enumerate(rows, start=1):
            kind = mount_type(row.footprint, footprint_roots)
            if not kind:
                no_type += 1
            if not row.mpn:
                no_mpn.extend(row.designators or [row.value])
            writer.writerow(
                [
                    i,
                    row.quantity,
                    ",".join(row.designators),
                    row.mpn,
                    row.manufacturer,
                    row.value,
                    row.package,
                    kind,
                ]
            )
    notes = []
    if no_mpn:
        shown = ", ".join(no_mpn[:8]) + (f", +{len(no_mpn) - 8} more" if len(no_mpn) > 8 else "")
        notes.append(f"{len(no_mpn)} part(s) have no MPN — PCBWay sources turnkey by MPN: {shown}")
    if no_type:
        notes.append(f"{no_type} line(s) have no SMD/THT type — the library did not say")
    return FabFile("pcbway-bom", str(out_csv), _row_count(out_csv), "; ".join(notes))


def build_assembly(
    bom_csv: Path,
    positions_csv: Path | None,
    out_dir: Path,
    *,
    house: str = "jlcpcb",
    footprint_roots: list[Path] | None = None,
    lcsc: bool = False,
    creds: CredResolver | None = None,
) -> AssemblyPackage:
    """Write one house's upload files. Never raises for a missing input."""
    if house not in HOUSES:
        return AssemblyPackage(False, house, reason=f"unknown assembly house {house!r}")
    bom_csv = Path(bom_csv)
    if not bom_csv.is_file():
        return AssemblyPackage(
            False, house, reason=f"no BOM at {bom_csv.name} — build the release package first"
        )
    out_dir = Path(out_dir) / house
    out_dir.mkdir(parents=True, exist_ok=True)
    pkg = AssemblyPackage(True, house)

    if house == "jlcpcb":
        try:
            bom_out = out_dir / "bom.csv"
            res = _translate("bom", [str(bom_csv), "-o", str(bom_out)])
        except KicadHappyMissing as exc:
            return AssemblyPackage(False, house, reason=str(exc))
        if not bom_out.is_file():
            return AssemblyPackage(False, house, reason=f"BOM translation failed: {res.error}")
        pkg.files.append(FabFile("jlcpcb-bom", str(bom_out), _row_count(bom_out)))

        if lcsc:
            rows = read_bom_csv(bom_csv)
            parts = resolve_lcsc([r.mpn for r in rows if r.mpn], creds=creds)
            filled, unbuildable = fill_lcsc_column(bom_out, parts)
            pkg.lcsc = [p.to_dict() for p in parts.values()]
            if unbuildable:
                shown = ", ".join(unbuildable[:8]) + (
                    f", +{len(unbuildable) - 8} more" if len(unbuildable) > 8 else ""
                )
                pkg.warnings.append(
                    f"{len(unbuildable)} part(s) have no LCSC part number and cannot be "
                    f"assembled by JLCPCB — order them separately or respecify: {shown}"
                )
            dead = [p.mpn for p in parts.values() if not p.orderable]
            if dead:
                pkg.warnings.append(
                    f"{len(dead)} resolved part(s) show zero LCSC stock: {', '.join(dead[:8])}"
                )
        else:
            pkg.warnings.append(
                "LCSC part numbers are blank — JLCPCB sources assembly by LCSC number, "
                "not MPN. Re-run with --lcsc to look them up."
            )
    else:
        roots = footprint_roots or []
        pkg.files.append(
            write_pcbway_bom(read_bom_csv(bom_csv), out_dir / "bom.csv", footprint_roots=roots)
        )
        note = pkg.files[-1].note
        if note:
            pkg.warnings.append(note)

    # The placement file is the same shape for both houses, so it is translated
    # once per house rather than reasoned about twice.
    if positions_csv and Path(positions_csv).is_file():
        cpl_out = out_dir / "cpl.csv"
        args = [str(positions_csv), "-o", str(cpl_out)]
        # Filtering against the BOM drops designators the BOM does not carry.
        # Both houses reject an upload whose CPL references parts the BOM never
        # mentioned, and the rejection names neither file.
        #
        # JLCPCB filters against its *translated* BOM, because translation drops
        # DNP rows — a part marked do-not-populate must leave the CPL too, or it
        # gets placed. PCBWay filters against the source BOM instead: its own
        # writer keeps every row, and the translator cannot read PCBWay's
        # columns anyway (it wants Value, PCBWay says Description).
        jlc_bom = out_dir / "bom.csv"
        if house == "jlcpcb" and jlc_bom.is_file():
            args += ["--bom", str(jlc_bom)]
        elif house == "pcbway":
            args += ["--bom", str(bom_csv)]
        try:
            res = _translate("pnp", args)
        except KicadHappyMissing as exc:
            pkg.warnings.append(str(exc))
            return pkg
        if cpl_out.is_file():
            stats = res.data if isinstance(res.data, dict) else {}
            orphans = int(stats.get("filtered_orphans") or 0)
            pkg.files.append(
                FabFile(
                    f"{house}-cpl",
                    str(cpl_out),
                    _row_count(cpl_out),
                    f"{orphans} placement row(s) dropped for having no BOM line" if orphans else "",
                )
            )
            if orphans:
                pkg.warnings.append(
                    f"{orphans} placed part(s) are absent from the BOM — they will not be assembled"
                )
        else:
            pkg.warnings.append(f"placement translation failed: {res.error}")
    else:
        pkg.warnings.append(
            "no placement file — assembly needs one; export it with the release package"
        )
    return pkg


# ---------------------------------------------------------------------------
# Sourcing gaps on the emitted board
# ---------------------------------------------------------------------------


def sourcing_gaps(schematic: Path, *, timeout: int = 180) -> dict:
    """What the emitted schematic is missing before anyone can order it.

    Runs kicad-happy's BOM analyzer over the board BLPL actually wrote, rather
    than over ``bom.json`` — the point is to catch fields the emitter dropped on
    the way, which an artifact-to-artifact check cannot see.
    """
    schematic = Path(schematic)
    if not schematic.is_file():
        return {"ok": True, "skipped": True, "reason": "no .kicad_sch — run stage6 first"}
    try:
        path = script_path("bom", "bom_manager.py")
    except KicadHappyMissing as exc:
        return {"ok": True, "skipped": True, "reason": str(exc)}
    if not path.is_file():
        return {"ok": True, "skipped": True, "reason": "this kicad-happy checkout has no bom_manager.py"}

    try:
        res = run_script(path, ["analyze", str(schematic), "--json"], timeout=timeout)
    except Exception as exc:
        return {"ok": False, "skipped": False, "reason": f"{type(exc).__name__}: {exc}"}
    if not isinstance(res.data, dict):
        return {"ok": False, "skipped": False, "reason": res.error}
    return {"ok": True, "skipped": False, **res.data}


def _json_default(obj):
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


def dumps(payload) -> str:
    return json.dumps(payload, indent=2, default=_json_default)
