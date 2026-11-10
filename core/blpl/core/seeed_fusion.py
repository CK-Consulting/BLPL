"""The files Seeed Fusion's PCBA order asks for, and a gerber zip any fab takes.

Seeed's order form has three upload fields and one text box, and each wants
something the generic package does not already hold in that shape:

- **PCB:** one zip of gerbers and drill files. The package keeps them loose in
  ``gerbers/``, which is right for a person and wrong for an upload field.
  :func:`zip_gerbers` writes ``<name>-gerbers.zip``; it is its own release
  option too, since most fabs' upload pages want the same zip.
- **BOM:** Seeed's own template, an .xlsx whose columns its system reads
  (``Reference Designator *``, ``Quantity *``, ``Manufacturer Part Number or
  Seeed SKU *`` ...). Seeed's notes on it: designators separated by commas only;
  for a Seeed Studio product the Seeed SKU goes in the MPN column, not the
  product name; only parts Seeed is to buy; a part bought but not assembled is
  marked DNP. The SKUs come from project.yaml ``assembly.seeed_fusion.sku``, by
  MPN, because nothing in a design's markdown or a distributor's catalogue says
  that a XIAO module's SKU is 101991422.
- **Assembly files:** a pick-and-place file and an assembly drawing, plus
  anything else that helps (photos, 3D). Seeed's KiCad instructions are the
  footprint position CSV, and a printed drawing of F.Fab and F.CrtYd for the
  top, B.Fab and B.CrtYd for the bottom, each on one page; zipped, under 50 MB.
- **MCU model:** a text box, so it goes into ``ORDER-NOTES.txt`` for whoever
  fills the form, from ``assembly.seeed_fusion.mcu``.

The .xlsx is written with the standard library. It is a zip of five small XML
parts, and the one consumer that matters (Seeed's importer) reads cell values,
not styling; a spreadsheet library in the release image for one sheet would be
a dependency carried for nothing.
"""

from __future__ import annotations

import csv
import io
import re
import subprocess
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from . import release as _release
from .release import Step

# Seeed's template, columns in order. The star marks a column Seeed requires.
BOM_COLUMNS = (
    "序号", "Reference Designator *", "Quantity *", "Manufacturer Part Number or Seeed SKU *",
    "Manufacturer", "Value / Specification", "Package / Footprint", "Link",
    "Alternative parts", "Remarks",
)

# Seeed's limit for each uploaded archive.
UPLOAD_LIMIT = 50 * 1024 * 1024

_TIMEOUT = 300


# -- config -----------------------------------------------------------------


def settings(config_path: Path | None) -> dict:
    """project.yaml ``assembly.seeed_fusion``, or nothing.

    Read from the board's project.yaml when the release runs, not carried
    through Stage 5: a SKU is ordering data, and a release built after someone
    adds one should not need the board emitted again to see it.
    """
    import yaml

    if config_path is None or not Path(config_path).is_file():
        return {}
    try:
        doc = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    found = ((doc.get("assembly") or {}).get("seeed_fusion") if isinstance(doc, dict) else None)
    return found if isinstance(found, dict) else {}


# -- the gerber zip ---------------------------------------------------------


def zip_gerbers(out_dir: Path, name: str) -> Step:
    """``<name>-gerbers.zip`` at the package root: gerbers and drill, flat.

    Flat because that is what a fab's upload expects to unpack; a zip holding
    one ``gerbers/`` folder has made more than one importer report an empty
    board.
    """
    src = Path(out_dir) / "gerbers"
    files = sorted(p for p in src.iterdir() if p.is_file()) if src.is_dir() else []
    if not files:
        return Step("gerber-zip", False, "no gerbers were exported to zip (see the gerbers step)")
    archive = Path(out_dir) / f"{name}-gerbers.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.name)
    drills = [p.name for p in files if p.suffix.lower() == ".drl"]
    detail = "" if drills else "no drill file in the zip; the fab will ask for one"
    return Step("gerber-zip", True, detail, [archive.name])


# -- the BOM ----------------------------------------------------------------


def _natural(ref: str) -> tuple:
    m = re.match(r"([A-Za-z_]*)(\d*)(.*)", ref)
    head, num, tail = m.groups() if m else (ref, "", "")
    return (head, int(num) if num else -1, tail)


def _bom_rows(bom_json: Path) -> list[dict]:
    import json

    rows = json.loads(Path(bom_json).read_text(encoding="utf-8"))
    if isinstance(rows, dict):
        rows = rows.get("components") or rows.get("rows") or []
    return [r for r in rows if isinstance(r, dict)]


def bom_lines(bom_json: Path, *, not_bought: set[str], hand_fit: set[str],
              sku: dict[str, str]) -> list[dict]:
    """The BOM grouped into Seeed's lines, in its columns.

    Left out: parts Seeed is not to buy (dnp, footprint_only). Kept but marked
    DNP in Remarks, as Seeed's notes ask for a part bought and not assembled:
    hand_fit parts, and parts that never land on the board (package
    ``not_placed``: an antenna on a cable, a cell). One part number from two
    makers stays two lines, and one part number from one maker is one line
    even where the BOM describes it two ways (the ear tag's 470 Ω resistors
    came out as two lines, one "470R" and one "Thick film chip resistor, 470
    Ω"): Seeed buys by part number, and two lines for one reel is two quotes.
    """
    from .symbol_resolution import is_not_placed

    grouped: dict[tuple, dict] = {}
    for row in _bom_rows(bom_json):
        ref = str(row.get("refdes") or row.get("local_id") or "").strip()
        if not ref or ref in not_bought:
            continue
        mpn = str(row.get("mpn") or row.get("part_hint") or "").strip()
        maker = str(row.get("manufacturer") or row.get("manufacturer_hint") or "").strip()
        seeed = str(sku.get(mpn) or "").strip() if mpn else ""
        part = seeed or mpn
        value = str(row.get("value") or row.get("description") or "").strip()
        package = str(row.get("package") or row.get("footprint") or row.get("package_hint")
                      or row.get("footprint_hint") or "").strip()
        off_board = any(is_not_placed(str(row.get(k) or ""))
                        for k in ("package", "package_hint", "footprint", "footprint_hint"))
        dnp = "hand" if ref in hand_fit else "off" if off_board else ""
        if off_board:
            package = ""  # "not_placed" is our flag, not a package
        # By part number where there is one; a line with none can only be
        # told apart by what it is.
        key = (part, maker, dnp) if part else ("", maker, value, package, dnp)
        line = grouped.setdefault(key, {
            "refs": [], "part": part, "maker": maker, "value": value, "package": package,
            "link": str(row.get("datasheet_url") or row.get("datasheet") or "").strip(),
            "remarks": [],
        })
        line["refs"].append(ref)
        if seeed and f"Seeed SKU for {mpn}" not in line["remarks"]:
            line["remarks"].append(f"Seeed SKU for {mpn}")
        if dnp and not any(r.startswith("DNP") for r in line["remarks"]):
            line["remarks"].append("DNP: purchase only, fitted by hand after assembly"
                                   if dnp == "hand" else
                                   "DNP: purchase only, not on the board (fitted at final assembly)")

    lines = sorted(grouped.values(), key=lambda e: _natural(sorted(e["refs"], key=_natural)[0]))
    out = []
    for i, e in enumerate(lines, 1):
        refs = sorted(e["refs"], key=_natural)
        out.append(dict(zip(BOM_COLUMNS, (
            i, ",".join(refs), len(refs), e["part"], e["maker"], e["value"], e["package"],
            e["link"], "", "; ".join(e["remarks"]),
        ))))
    return out


def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def write_xlsx(path: Path, sheet: str, header: tuple[str, ...], rows: list[dict]) -> None:
    """One sheet, a bold header and inline strings: what an importer reads."""
    def cell(ref: str, value, style: int = 0) -> str:
        s = f' s="{style}"' if style else ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f'<c r="{ref}"{s}><v>{value}</v></c>'
        text = escape(str(value))
        return f'<c r="{ref}" t="inlineStr"{s}><is><t xml:space="preserve">{text}</t></is></c>'

    body = ['<row r="1">' + "".join(cell(f"{_col(j)}1", h, 1) for j, h in enumerate(header, 1))
            + "</row>"]
    for i, row in enumerate(rows, 2):
        body.append(f'<row r="{i}">' + "".join(
            cell(f"{_col(j)}{i}", row.get(h, "")) for j, h in enumerate(header, 1)
            if row.get(h, "") != "") + "</row>")
    widths = "".join(f'<col min="{j}" max="{j}" width="{w}" customWidth="1"/>'
                     for j, w in enumerate((6, 28, 10, 32, 22, 40, 24, 30, 18, 40), 1)
                     if j <= len(header))
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            "</Types>"),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{escape(sheet)}" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>"),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>"),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<cols>{widths}</cols><sheetData>{''.join(body)}</sheetData></worksheet>"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for part, xml in parts.items():
            zf.writestr(part, xml)


def export_bom(bom_json: Path, out: Path, *, not_bought: set[str], hand_fit: set[str],
               sku: dict[str, str]) -> tuple[Step, list[dict]]:
    if not Path(bom_json).is_file():
        return Step("seeed-bom", False, "no BOM yet: run the pipeline through Stage 1"), []
    try:
        lines = bom_lines(bom_json, not_bought=not_bought, hand_fit=hand_fit, sku=sku)
    except (OSError, ValueError) as exc:
        return Step("seeed-bom", False, f"could not read {Path(bom_json).name}: {exc}"), []
    write_xlsx(out, "BOM", BOM_COLUMNS, lines)
    part_col = BOM_COLUMNS[3]
    missing = [ln[BOM_COLUMNS[1]] for ln in lines if not ln[part_col]]
    unused = sorted(set(sku) - {m for m in sku if any(f"Seeed SKU for {m}" in ln["Remarks"]
                                                         for ln in lines)})
    notes = []
    if missing:
        notes.append(f"{len(missing)} line(s) have no part number ({'; '.join(missing)}); "
                     "Seeed will ask about them")
    if unused:
        notes.append("assembly.seeed_fusion.sku names MPNs not in this BOM: " + ", ".join(unused))
    return Step("seeed-bom", True, "; ".join(notes), [out.name]), lines


# -- the assembly files -----------------------------------------------------


def pick_and_place(positions: Path, out: Path, skip: set[str]) -> tuple[Step, bool]:
    """KiCad's own footprint position CSV, as Seeed's KiCad instructions ask for,
    less the parts Seeed does not place. Returns the step and whether any part
    is on the bottom side."""
    if not Path(positions).is_file():
        return Step("seeed-pick-and-place", False,
                    "no placement file was exported (see the placement step)"), False
    with Path(positions).open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        rows = [r for r in reader if r]
    try:
        ref_i, side_i = header.index("Ref"), header.index("Side")
    except ValueError:
        return Step("seeed-pick-and-place", False,
                    f"{Path(positions).name} has no Ref/Side columns; not KiCad's CSV"), False
    kept = [r for r in rows if r[ref_i] not in skip]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(header)
        w.writerows(kept)
    bottom = any(r[side_i].strip().lower() == "bottom" for r in kept)
    return Step("seeed-pick-and-place", True, "", [out.name]), bottom


def _cli(cmd: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run: {exc}"
    text = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, text.strip()[:400] or f"exit {proc.returncode}"
    return True, text


def assembly_drawing(cli: str, pcb: Path, out: Path, side: str) -> Step:
    """One side's drawing on one page: fab and courtyard layers and the outline.

    The fab layer carries the reference designators and the pin-1 and polarity
    marks; pads are sketched on it so a pin 1 drawn as a pad shape shows too.
    Values are left off: on a dense board they overprint the designators, which
    are what the drawing is for. The bottom is mirrored, as it is seen.
    """
    f = "F" if side == "top" else "B"
    cmd = [cli, "pcb", "export", "pdf", "--output", str(out),
           "--layers", f"{f}.Fab,{f}.CrtYd,Edge.Cuts", "--mode-single",
           "--sketch-pads-on-fab-layers", "--crossout-DNP-footprints-on-fab-layers",
           "--exclude-value", "--include-border-title", "--scale", "0"]
    if side == "bottom":
        cmd.append("--mirror")
    ok, text = _cli([*cmd, str(pcb)])
    name = f"seeed-drawing-{side}"
    if not ok:
        return Step(name, False, text)
    if not out.is_file() or out.stat().st_size == 0:
        return Step(name, False, f"kicad-cli reported success but wrote no {out.name}")
    return Step(name, True, "", [out.name])


def step_model(cli: str, pcb: Path, out: Path) -> Step:
    """A 3D model of the assembled board, which Seeed lists as helpful.

    A footprint whose 3D model is not in the libraries is simply absent from
    the STEP, and kicad-cli still succeeds; so the parts it could not add are
    named, or the model would look complete while missing a module.
    """
    ok, text = _cli([cli, "pcb", "export", "step", "--output", str(out), "--subst-models",
                     "--force", "--no-dnp", str(pcb)])
    if not ok:
        return Step("seeed-3d", False, text)
    if not out.is_file():
        return Step("seeed-3d", False, f"kicad-cli reported success but wrote no {out.name}")
    missing = sorted(set(re.findall(r"Could not add 3D model for (\S+?)\.", text)), key=_natural)
    detail = (f"no 3D model in the libraries for {', '.join(missing)}; they are absent from "
              "the STEP" if missing else "")
    return Step("seeed-3d", True, detail, [out.name])


# -- the whole Seeed package ------------------------------------------------


def export(*, out_dir: Path, name: str, pcb: Path | None, bom_json: Path,
           config_path: Path | None, not_bought: set[str], hand_fit: set[str],
           kicad_cli: str | None = None) -> list[Step]:
    """``seeed/`` in the release: the BOM, the assembly zip and the order notes.

    The gerber zip it needs is the package's own (:func:`zip_gerbers`), which
    ``build`` writes whenever this runs.
    """
    out_dir = Path(out_dir)
    seeed = out_dir / "seeed"
    seeed.mkdir(parents=True, exist_ok=True)
    cfg = settings(config_path)
    sku = {str(k): str(v) for k, v in (cfg.get("sku") or {}).items()} if isinstance(
        cfg.get("sku"), dict) else {}
    mcu = str(cfg.get("mcu") or "").strip()

    steps: list[Step] = []
    bom_step, lines = export_bom(bom_json, seeed / f"{name}-seeed-bom.xlsx",
                                 not_bought=not_bought, hand_fit=hand_fit, sku=sku)
    steps.append(bom_step)

    # Seeed buys a hand_fit part and does not place it, so it is in the BOM
    # (as DNP) and out of the placement file, like everything not bought.
    staging = seeed / "assembly"
    staging.mkdir(parents=True, exist_ok=True)
    pnp_step, bottom = pick_and_place(out_dir / "placement" / "positions.csv",
                                      staging / f"{name}-pick-and-place.csv",
                                      not_bought | hand_fit)
    steps.append(pnp_step)

    # Looked up through the module, so a test or caller that replaces
    # release.find_kicad_cli replaces it here too.
    cli = kicad_cli or _release.find_kicad_cli()
    if pcb is None or cli is None:
        why = "no board for this project yet" if pcb is None else "kicad-cli not found"
        steps.append(Step("seeed-drawing-top", False, why))
    else:
        steps.append(assembly_drawing(cli, pcb, staging / f"{name}-assembly-top.pdf", "top"))
        if bottom:
            steps.append(assembly_drawing(cli, pcb, staging / f"{name}-assembly-bottom.pdf",
                                          "bottom"))
        steps.append(step_model(cli, pcb, staging / f"{name}.step"))

    archive = seeed / f"{name}-seeed-assembly.zip"
    files = sorted(p for p in staging.iterdir() if p.is_file())
    if not files:
        steps.append(Step("seeed-assembly", False, "nothing to zip: no placement file or drawing"))
    else:
        def write(paths: list[Path]) -> None:
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in paths:
                    zf.write(p, p.name)
        write(files)
        detail = ""
        if archive.stat().st_size > UPLOAD_LIMIT:
            # The 3D model is the only optional file and the only large one.
            write([p for p in files if p.suffix.lower() != ".step"])
            detail = "the 3D model took the zip over Seeed's 50 MB limit and was left out"
            if archive.stat().st_size > UPLOAD_LIMIT:
                steps.append(Step("seeed-assembly", False,
                                  "the assembly zip is over Seeed's 50 MB upload limit"))
                files = []
        if files:
            steps.append(Step("seeed-assembly", True, detail, [archive.name]))
    # The staged files stay in the package beside the zip, so the drawing can
    # be opened without unzipping anything.

    steps.append(check(seeed, staging, lines, name))
    (seeed / "ORDER-NOTES.txt").write_text(
        _order_notes(name, mcu, lines, sku, bottom, steps), encoding="utf-8")
    return steps


def _readable(path: Path) -> str | None:
    """Why ``path`` would not open, or None. Seeed asks that every file be
    opened and confirmed readable before it is packaged."""
    try:
        if path.suffix.lower() in (".zip", ".xlsx"):
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
                if bad:
                    return f"{path.name}: {bad} is corrupt"
                if path.suffix.lower() == ".xlsx" and "xl/worksheets/sheet1.xml" not in zf.namelist():
                    return f"{path.name}: no worksheet"
        elif path.suffix.lower() == ".pdf":
            data = path.read_bytes()
            if not data.startswith(b"%PDF") or b"%%EOF" not in data[-1024:]:
                return f"{path.name}: not a complete PDF"
        elif path.suffix.lower() == ".csv":
            with path.open(newline="", encoding="utf-8") as f:
                if len(list(csv.reader(f))) < 2:
                    return f"{path.name}: no rows"
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        return f"{path.name}: {exc}"
    return None


def check(seeed: Path, staging: Path, lines: list[dict], name: str) -> Step:
    """Seeed's own checklist, the parts a program can answer.

    - The BOM and the pick-and-place file are from the same revision, with
      matching designators: every part the BOM has Seeed place is in the
      placement file, and the placement file places nothing the BOM does not
      buy. A part in one and not the other is a question on the quote at best,
      and an unplaced or unbought part at worst.
    - Every file opens.
    """
    problems: list[str] = []
    placed: set[str] = set()
    pnp = staging / f"{name}-pick-and-place.csv"
    if pnp.is_file():
        with pnp.open(newline="", encoding="utf-8") as f:
            placed = {r["Ref"] for r in csv.DictReader(f) if r.get("Ref")}
        # What the BOM has Seeed place: every line not marked DNP.
        bought = {ref for ln in lines if "DNP:" not in ln["Remarks"]
                  for ref in ln[BOM_COLUMNS[1]].split(",") if ref}
        if bought - placed:
            problems.append("in the BOM but not the pick-and-place file: "
                            + ", ".join(sorted(bought - placed, key=_natural)))
        if placed - bought:
            problems.append("in the pick-and-place file but not the BOM: "
                            + ", ".join(sorted(placed - bought, key=_natural)))
    else:
        # No placement file is not zero placements: nothing was compared, so
        # the check cannot say the two agree.
        problems.append(f"no pick-and-place file ({pnp.name}) to compare with the BOM")
    files = [p for p in (*seeed.iterdir(), *staging.iterdir()) if p.is_file()
             and p.suffix.lower() in (".zip", ".xlsx", ".pdf", ".csv")]
    problems += [why for why in map(_readable, sorted(files)) if why]
    if problems:
        return Step("seeed-check", False, "; ".join(problems))
    return Step("seeed-check", True, f"BOM and pick-and-place agree on {len(placed)} placed "
                                     f"parts; {len(files)} files reopened")


def _order_notes(name: str, mcu: str, lines: list[dict], sku: dict[str, str],
                 bottom: bool, steps: list[Step]) -> str:
    dnp = [ln[BOM_COLUMNS[1]] for ln in lines if ln["Remarks"].find("DNP") >= 0]
    used = {m: s for m, s in sku.items()
            if any(f"Seeed SKU for {m}" in ln["Remarks"] for ln in lines)}
    out = [
        f"Seeed Fusion PCBA order: {name}",
        "",
        "Upload fields on the order page:",
        f"  PCB (gerber files)  ->  ../{name}-gerbers.zip",
        f"  BOM                 ->  {name}-seeed-bom.xlsx",
        f"  Assembly files      ->  {name}-seeed-assembly.zip",
        "                          (pick-and-place CSV, assembly drawing"
        + (" top and bottom" if bottom else ", top") + ", 3D model)",
        "",
        "MCU model used in the project: "
        + (mcu or "NOT SET - add assembly.seeed_fusion.mcu to project.yaml, or type it on the form"),
    ]
    if used:
        out += ["", "Seeed SKUs in the BOM's part number column:"]
        out += [f"  {s}  ({m})" for m, s in sorted(used.items())]
    if dnp:
        out += ["", "Bought by Seeed but not assembled (DNP in the BOM): " + "; ".join(dnp)]
    failed = [s for s in steps if not s.ok]
    noted = [s for s in steps if s.ok and s.detail]
    if failed or noted:
        out += ["", "Check before uploading:"]
        out += [f"  {s.name}: FAILED - {s.detail}" for s in failed]
        out += [f"  {s.name}: {s.detail}" for s in noted]
    return "\n".join(out) + "\n"
