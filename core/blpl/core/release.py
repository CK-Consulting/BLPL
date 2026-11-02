"""The package a contract fab can quote from, and the gate that says it may be sent.

This is where the pipeline's purpose finally lands. Everything upstream produces
a KiCad project; a PCB house cannot quote a KiCad project. They quote gerbers, a
drill file, a placement file, and a BOM — and they quote them against a board
somebody has decided is finished.

Two halves, deliberately in that order:

**The gate runs first, and can refuse.** kicad-happy's ``fab_release_gate``
reads the analyzer JSON Stage 8 already produced and answers "is this ready".
Exporting first and gating second would put a downloadable, sendable zip of an
unfinished board on disk, and the entire failure mode this codebase guards
against is a board that *looks* finished — every net in a BLPL board ships
unrouted, and the project still opens and renders beautifully.

A failed gate does not stop the export outright, because a rejected board is
exactly when you want the gerbers to look at. It marks the package instead:
``READ-ME-FIRST.txt`` in the zip and a refusal recorded in the manifest, so the
package cannot be mistaken for a release by whoever finds it later.

**Everything is checksummed and listed.** The manifest names every file with its
size and SHA-256, plus the tool versions that made it. A fab quoting from a zip
whose provenance nobody can reconstruct is how the wrong revision gets built.

Nothing here is silently skipped: an export step that fails is recorded with the
kicad-cli message, and the package says which outputs are missing.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from blpl.emitter import sexpr

# The non-copper layers a fab needs, in the order a fab expects to find them.
# Named explicitly rather than left to kicad-cli's default because the default
# has changed between versions, and a package silently missing a mask layer is
# a re-quote at best. Copper comes from the board itself (copper_layers): a
# fixed F.Cu,B.Cu here shipped a 4-layer board's package without its inner
# layers, and nothing about the package said so.
FAB_LAYERS = "F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts"

# Layers a `fabrication:` colour of `none` takes out of the gerbers. A board
# ordered with no mask but shipped with mask gerbers contradicts itself, and
# the fab's CAM believes the gerbers over board-specs.md (Codex, PR #98).
_NONE_DROPS = {"mask_color": ("F.Mask", "B.Mask"), "silk_color": ("F.Silkscreen", "B.Silkscreen")}


def fab_layers(fab: dict | None = None) -> str:
    """FAB_LAYERS, less the mask or silk layers project.yaml says the board has none of."""
    fab = fab or {}
    drop = {lay for key, lays in _NONE_DROPS.items() if fab.get(key) == "none" for lay in lays}
    return ",".join(lay for lay in FAB_LAYERS.split(",") if lay not in drop)


def _fabrication(hdm_path: Path | None) -> dict:
    """hdm.yaml's `fabrication`, or nothing when there is no readable one."""
    import yaml

    if hdm_path is None or not hdm_path.is_file():
        return {}
    try:
        doc = yaml.safe_load(hdm_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    fab = doc.get("fabrication") if isinstance(doc, dict) else None
    return fab if isinstance(fab, dict) else {}

_COPPER_TYPES = {"signal", "power", "mixed", "jumper"}

_TIMEOUT = 300


@dataclass
class Step:
    """One export step, and whether it produced anything."""

    name: str
    ok: bool
    detail: str = ""
    outputs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"step": self.name, "ok": self.ok, "detail": self.detail, "outputs": self.outputs}


def copper_layers(pcb: Path) -> list[str]:
    """The copper layers ``pcb``'s layer table enables, top to bottom.

    Read from the board rather than from the stackup in project.yaml, because
    the board is what gets fabricated: if the two disagree, the gerbers have
    to describe the board. Empty when the file has no readable layer table,
    which the caller treats as a failure rather than guessing two layers.
    """
    try:
        root = sexpr.parse(pcb.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, IndexError):
        return []
    table = sexpr.find(root, "layers") if isinstance(root, list) else None
    names = [
        sexpr.unquote(row[1]) for row in (table or [])[1:]
        if isinstance(row, list) and len(row) >= 3 and row[2] in _COPPER_TYPES
        and sexpr.unquote(row[1]).endswith(".Cu")
    ]

    def order(name: str) -> int:
        if name == "F.Cu":
            return 0
        if name == "B.Cu":
            return 1_000
        m = re.fullmatch(r"In(\d+)\.Cu", name)
        return int(m.group(1)) if m else 999

    return sorted(names, key=order)


def find_kicad_cli() -> str | None:
    for candidate in (
        shutil.which("kicad-cli"),
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
        "/usr/bin/kicad-cli",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _tool_version(cli: str) -> str:
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return ((out.stdout or "") + (out.stderr or "")).strip().splitlines()[0] if out else "unknown"


def _run(cmd: list[str], name: str, expect_dir: Path | None = None) -> Step:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return Step(name, False, f"could not run: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400] or f"exit {proc.returncode}"
        return Step(name, False, detail)
    outputs: list[str] = []
    if expect_dir and expect_dir.is_dir():
        outputs = sorted(p.name for p in expect_dir.iterdir() if p.is_file())
    return Step(name, True, "", outputs)


# ---------------------------------------------------------------------------
# Fabrication outputs
# ---------------------------------------------------------------------------


def export_fabrication(pcb: Path, out_dir: Path, *, kicad_cli: str | None = None,
                       fab: dict | None = None) -> list[Step]:
    """Gerbers, drill files and a placement file, via kicad-cli.

    ``fab`` is the board's `fabrication:`; a mask or silk colour of ``none``
    leaves those layers out of the gerbers.

    Each is its own step so a failure in one does not hide the others: a board
    whose drill export failed still has gerbers worth looking at, and the
    manifest has to say precisely which piece is missing.
    """
    cli = kicad_cli or find_kicad_cli()
    if cli is None:
        return [
            Step(
                "fabrication",
                False,
                "kicad-cli not found — install KiCad, or run the release inside the container "
                "image, which has it",
            )
        ]

    gerbers = out_dir / "gerbers"
    gerbers.mkdir(parents=True, exist_ok=True)
    copper = copper_layers(pcb)
    if copper:
        gerber_step = _run(
            [cli, "pcb", "export", "gerbers", "--output", str(gerbers),
             "--layers", ",".join([*copper, fab_layers(fab)]), str(pcb)],
            "gerbers",
            gerbers,
        )
    else:
        gerber_step = Step("gerbers", False,
                           f"{pcb.name} has no readable copper layer table; not guessing "
                           "which copper layers to export")
    steps = [
        gerber_step,
        _run(
            [cli, "pcb", "export", "drill", "--output", str(gerbers),
             "--format", "excellon", "--excellon-separate-th", str(pcb)],
            "drill",
            gerbers,
        ),
    ]

    placement = out_dir / "placement"
    placement.mkdir(parents=True, exist_ok=True)
    steps.append(
        _run(
            [cli, "pcb", "export", "pos", "--output", str(placement / "positions.csv"),
             "--format", "csv", "--units", "mm", "--side", "both", str(pcb)],
            "placement",
            placement,
        )
    )
    return steps


# What an ODB++ job has to hold for a fab's CAM to read it: the layer matrix,
# the job's own record, and at least one step with an outline, its EDA data and
# a layer with features in it. kicad-cli exits 0 on a board it could not make
# sense of, so the archive is opened and looked at rather than trusted.
_ODB_REQUIRED = ("matrix/matrix", "misc/info")


def _odb_problem(archive: Path) -> str | None:
    """Why ``archive`` is not a usable ODB++ job, or None when it is."""
    try:
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        return f"kicad-cli wrote {archive.name}, but it is not a readable zip: {exc}"
    missing = [n for n in _ODB_REQUIRED if not any(x.endswith(n) for x in names)]
    steps = {x.split("/")[x.split("/").index("steps") + 1]
             for x in names if "steps" in x.split("/")[:-1]}
    for step in sorted(steps):
        has = lambda tail: any(f"steps/{step}/{tail}" in x for x in names)
        if has("profile") and has("eda/data") and any(
                f"steps/{step}/layers/" in x and x.endswith("/features") for x in names):
            break
    else:
        missing.append("a step with a profile, EDA data and layer features")
    return f"{archive.name} is missing " + ", ".join(missing) if missing else None


def export_odb(pcb: Path, out_dir: Path, name: str, *, kicad_cli: str | None = None) -> Step:
    """The board as an ODB++ job, zipped, for a fab whose CAM takes ODB++.

    One file instead of a folder of gerbers, drill and placement that the fab
    has to reassemble: layers, stackup, components and nets travel together.
    Precision is six decimal places of a millimetre. kicad-cli's default is
    two, which rounds every coordinate to 10 um and moved a pad of the test
    board by a micron; a fab will build exactly what this says.
    """
    cli = kicad_cli or find_kicad_cli()
    if cli is None:
        return Step("odb", False, "kicad-cli not found; ODB++ needs KiCad 9 or later")
    dest = out_dir / "odb" / f"{name}-odb.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    step = _run([cli, "pcb", "export", "odb", "--output", str(dest), "--compression", "zip",
                 "--units", "mm", "--precision", "6", str(pcb)], "odb")
    if not step.ok:
        return step
    if not dest.is_file():
        return Step("odb", False, f"kicad-cli reported success but wrote no {dest.name}")
    problem = _odb_problem(dest)
    if problem:
        return Step("odb", False, problem)
    return Step("odb", True, "", [dest.name])


# The column names CircuitHub's BOM import recognises
# (docs.circuithub.com, "Supplying a BOM as a CSV spreadsheet"). It needs the
# first two; the rest help it reconcile, and DNP keeps a part off the order.
_CIRCUITHUB_BOM = ("Reference Designators", "Manufacturer Part Number", "Manufacturer",
                   "Description", "Value", "DNP")


def export_circuithub(kicad_dir: Path | None, bom_json: Path, out_dir: Path, name: str,
                      not_fitted: "set[str] | None" = None) -> Step:
    """A design package for CircuitHub: the native project and a BOM, zipped.

    CircuitHub imports a KiCad project natively (KiCad 6 to 10) and turns down
    gerbers, which lose the part numbers and polarity it reads from the
    schematic. What it asks for is one .kicad_pro, one .kicad_pcb and the
    .kicad_sch files, from one design only, and optionally a BOM it reconciles
    against; so this is those, from the project Stage 6 published, and no
    library folders. A part not fitted by the assembler (dnp, footprint_only
    or hand_fit in project.yaml) is marked DNP so it is neither bought nor
    placed, as the JLCPCB and PCBWay files leave it out.
    """
    src = Path(kicad_dir) if kicad_dir is not None else None
    if src is None or not src.is_dir():
        return Step("circuithub", False, "no published KiCad project for this board yet: run the "
                                         "pipeline through Stage 6 (it publishes to kicad/)")
    found = {ext: sorted(p for p in src.iterdir() if p.is_file() and p.suffix == ext)
             for ext in (".kicad_pro", ".kicad_pcb", ".kicad_sch")}
    wrong = [f"{len(found[e])} {e} files" for e in (".kicad_pro", ".kicad_pcb") if len(found[e]) != 1]
    if not found[".kicad_sch"]:
        wrong.append("no .kicad_sch")
    if wrong:
        # CircuitHub takes one design per upload; guessing which board is
        # meant would be shipping the wrong one.
        return Step("circuithub", False, f"{src.name}/ holds {', '.join(wrong)}; CircuitHub needs "
                                         "exactly one project and board, and its schematics")

    try:
        rows = json.loads(Path(bom_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Step("circuithub", False, f"could not read {Path(bom_json).name}: {exc}")
    if isinstance(rows, dict):
        rows = rows.get("components") or rows.get("rows") or []
    grouped: dict[tuple, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ref = str(row.get("refdes") or row.get("local_id") or "").strip()
        if not ref:
            continue
        mpn = str(row.get("mpn") or row.get("part_hint") or "").strip()
        value = str(row.get("value") or "").strip()
        dnp = "dnp" if ref in (not_fitted or set()) else ""
        maker = str(row.get("manufacturer") or row.get("manufacturer_hint") or "").strip()
        # The manufacturer is part of what is ordered: one MPN from two makers
        # is two lines, or one of them silently becomes the other (Codex, PR #97).
        entry = grouped.setdefault((mpn, value, maker, dnp), {
            "refs": [], "Manufacturer Part Number": mpn, "Value": value, "DNP": dnp,
            "Manufacturer": maker, "Description": str(row.get("description") or ""),
        })
        entry["refs"].append(ref)

    pkg = out_dir / "circuithub"
    pkg.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CIRCUITHUB_BOM, extrasaction="ignore")
    writer.writeheader()
    for e in sorted(grouped.values(), key=lambda e: (e["DNP"], e["Manufacturer Part Number"], e["Value"])):
        writer.writerow({**e, "Reference Designators": ",".join(sorted(e["refs"]))})
    archive = pkg / f"{name}-circuithub.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in found[".kicad_pro"] + found[".kicad_pcb"] + found[".kicad_sch"]:
            zf.write(path, path.name)
        zf.writestr(f"{name}-bom.csv", buf.getvalue())

    missing = sum(1 for (mpn, _v, _m, dnp) in grouped if not mpn and not dnp)
    detail = (f"{missing} fitted line item(s) have no MPN; CircuitHub will ask about them"
              if missing else "")
    return Step("circuithub", True, detail, [archive.name])


# -- board-specs.md ----------------------------------------------------------

_MIL = 0.0254
_VIA_TYPES = {
    "none": "untented: vias left open both sides",
    "tented": "tented, IPC-4761 Type I",
    "plugged": "plugged, IPC-4761 Type III",
    "filled": "filled with non-conductive paste, IPC-4761 Type V",
    "filled_capped": "filled and capped (plated over), IPC-4761 Type VII, for via-in-pad",
}


def _walk(node):
    """Every list under ``node``, depth first."""
    if isinstance(node, list):
        yield node
        for child in node:
            yield from _walk(child)


def _board_minimums(pcb: Path | None) -> dict:
    """The smallest track width and hole the board actually has, in mm.

    Measured on the board rather than taken from the net classes, because the
    fab quotes what it is asked to make: a 0.1 mm neck-down in a footprint is
    0.1 mm, whatever the Default class says. A board not routed yet has no
    tracks, and that is said rather than reported as no minimum.
    """
    out: dict = {"trace": None, "drill": None, "drill_kind": None}
    if pcb is None or not pcb.is_file():
        return out
    try:
        tree = sexpr.parse(pcb.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for n in _walk(tree):
        head = n[0] if n else None
        if head in ("segment", "arc"):
            w = sexpr.find(n, "width")
            if w and len(w) > 1:
                width = float(w[1])
                out["trace"] = width if out["trace"] is None else min(out["trace"], width)
        elif head in ("via", "pad"):
            d = sexpr.find(n, "drill")
            if not d or len(d) < 2:
                continue
            sizes = [float(x) for x in d[1:] if isinstance(x, str) and _is_number(x)]
            if not sizes or min(sizes) <= 0:
                continue
            hole = min(sizes)
            if out["drill"] is None or hole < out["drill"]:
                out["drill"], out["drill_kind"] = hole, head
    return out


def _is_number(x: str) -> bool:
    try:
        float(x)
    except ValueError:
        return False
    return True


def _mm(v: float) -> str:
    return f"{v:.3f} mm ({v / _MIL:.1f} mil)"


def _colour(value, layers: str) -> str:
    """A mask or silk colour, saying for `none` that its gerbers were left out."""
    if value == "none":
        return f"none: no {layers}, and no {layers} gerbers in this package"
    return str(value or "")


def write_board_specs(pcb: Path | None, hdm_path: Path | None, out_dir: Path, *,
                      board: str, impedance_sheet: bool) -> Step:
    """board-specs.md at the package root, or a failed step saying why not.

    A hand-edited hdm.yaml can hold anything YAML parses: a list where the
    project should be, a word where a number should be. The release must
    still be written, refusal notice and all, so a value this cannot read
    fails this one step rather than the whole build (Codex, PR #98).
    """
    try:
        return _write_board_specs(pcb, hdm_path, out_dir, board=board,
                                  impedance_sheet=impedance_sheet)
    except (AttributeError, TypeError, ValueError, KeyError, IndexError) as exc:
        name = hdm_path.name if hdm_path is not None else "hdm.yaml"
        return Step("board-specs", False,
                    f"{name} has board or fabrication settings that do not read: {exc!r}")


def _write_board_specs(pcb: Path | None, hdm_path: Path | None, out_dir: Path, *,
                       board: str, impedance_sheet: bool) -> Step:
    """board-specs.md at the package root: what the bare board is built from.

    The quoting sites take gerbers, or a KiCad project they mostly do not read
    the stackup of, and ask for the rest in a form: layers, thickness, copper
    weight, laminate, Tg, finish, colours, gold fingers, via protection,
    castellation, and the smallest trace, space and drill. This is that form,
    filled in once, from project.yaml (project.stackup, fabrication and the
    net classes) and from the board itself. Whatever project.yaml leaves out
    is said to be left out: "not specified" means the fab's standard applies,
    and "(default)" marks the value BLPL assumed and wrote into the board.
    """
    import yaml

    if hdm_path is None or not hdm_path.is_file():
        return Step("board-specs", False, "no hdm.yaml for this board: run the pipeline through "
                                          "Stage 5 so the board's stackup and fabrication are known")
    try:
        hdm = yaml.safe_load(hdm_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return Step("board-specs", False, f"could not read {hdm_path.name}: {exc}")
    if not isinstance(hdm, dict):
        return Step("board-specs", False, f"{hdm_path.name} is not a mapping")
    project = hdm.get("project") or {}
    st = project.get("stackup") or {}
    fab = hdm.get("fabrication") or {}
    classes = hdm.get("net_classes") or {}
    layers = int(st.get("layers") or 2)
    unset: list[str] = []

    def given(key: str, label: str, text) -> str:
        if key in fab:
            return text
        unset.append(label)
        return "not specified"

    oz = fab.get("copper_oz") or {}
    outer = f"{oz['outer']:g} oz" if oz.get("outer") else "1 oz (default)"
    inner = f"{oz['inner']:g} oz" if oz.get("inner") else "0.5 oz (default)"
    dims = project.get("dimensions") or []
    fingers = fab.get("gold_fingers")
    if isinstance(fingers, dict):
        bevel = fingers.get("bevel_deg") or 0
        fingers_text = f"yes, hard gold, bevelled {bevel} degrees" if bevel else "yes, hard gold, no bevel"
    else:
        fingers_text = given("gold_fingers", "gold fingers", "none")
    rows = [
        ("Board", project.get("board_id") or project.get("name") or board),
        ("Copper layers", str(layers)),
        ("Board size", f"{float(dims[0]):.2f} x {float(dims[1]):.2f} mm" if len(dims) == 2 else "not specified"),
        ("Finished thickness", f"{float(st['thickness']):g} mm" if st.get("thickness") else "1.6 mm (default)"),
        ("Laminate", fab.get("material") or "FR-4 (default)"),
        ("Glass transition (Tg)", given("tg_c", "Tg", f"{fab.get('tg_c', 0):g} C minimum")),
        ("Copper weight, outer layers", outer),
    ]
    if layers > 2:
        rows.append(("Copper weight, inner layers", inner))
    rows += [
        ("Surface finish", st.get("finish") or "not specified"),
        ("Solder mask colour", given("mask_color", "solder mask colour", _colour(fab.get("mask_color"), "mask"))),
        ("Silkscreen colour", given("silk_color", "silkscreen colour", _colour(fab.get("silk_color"), "silkscreen"))),
        ("Gold fingers", fingers_text),
        ("Via protection", given("via_protection", "via protection",
                                 _VIA_TYPES.get(str(fab.get("via_protection")), ""))),
        ("Castellated edges", given("castellated", "castellated edges",
                                    "yes, plated half-holes on the board edge" if fab.get("castellated") else "no")),
        ("Impedance control", "yes: see fab/impedance.md" if impedance_sheet else "no"),
    ]
    if not st.get("finish"):
        unset.append("surface finish")

    measured = _board_minimums(pcb)
    trace_classes = [float(c["trace_width"]) for c in classes.values()
                     if isinstance(c, dict) and c.get("trace_width")]
    if measured["trace"] is not None:
        rows.append(("Minimum trace width", _mm(measured["trace"]) + ", measured on the board"))
    elif trace_classes:
        rows.append(("Minimum trace width", _mm(min(trace_classes)) +
                     ", from the net classes: the board has no tracks yet"))
    else:
        rows.append(("Minimum trace width", "not known: no tracks and no net class widths"))
    clear = [float(c["clearance"]) for c in classes.values()
             if isinstance(c, dict) and c.get("clearance")]
    rows.append(("Minimum spacing", _mm(min(clear)) + ", the smallest net class clearance"
                 if clear else "not known: no net class clearances"))
    if measured["drill"] is not None:
        kind = "a via" if measured["drill_kind"] == "via" else "a pad hole"
        rows.append(("Minimum drill", _mm(measured["drill"]) + f", measured on the board ({kind})"))
    else:
        vias = [float(c["via_drill"]) for c in classes.values()
                if isinstance(c, dict) and c.get("via_drill")]
        rows.append(("Minimum drill", _mm(min(vias)) + ", from the net classes: the board has no holes yet"
                     if vias else "not known: no holes and no net class via drills"))

    lines = [f"# Board specifications: {rows[0][1]}", "",
             "What this board is to be built from, for the quote form. Taken from the "
             "project's project.yaml and measured on the board in kicad/.", "",
             "| Item | Specification |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    if unset:
        lines += ["", "## Not specified", "",
                  "project.yaml does not state these, so the fab's standard applies: "
                  + ", ".join(unset) + ". Set them under fabrication in project.yaml "
                  "(Project settings, Board) when they matter."]
    path = out_dir / "board-specs.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Step("board-specs", True,
                f"{len(unset)} item(s) not specified, left to the fab's standard" if unset else "",
                [path.name])


def _not_bought(pipeline: Path, hdm: Path | None = None) -> set[str]:
    """Parts the HDM marks DNP or footprint-only (project.yaml assembly)."""
    return _assembled_as(pipeline, hdm, ("dnp", "footprint_only"))


def _hand_fit(pipeline: Path, hdm: Path | None = None) -> set[str]:
    """Parts bought but fitted by hand after assembly (project.yaml assembly)."""
    return _assembled_as(pipeline, hdm, ("hand_fit",))


def _assembled_as(pipeline: Path, hdm: Path | None, kinds: tuple[str, ...]) -> set[str]:
    import yaml

    hdm = hdm or pipeline / "hdm.yaml"
    try:
        doc = yaml.safe_load(hdm.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return set()
    # A malformed HDM is the release validation's to refuse, with a reason;
    # crashing here on a list root aborted the release before it got there
    # (Codex, PR #52).
    comps = doc.get("components") if isinstance(doc, dict) else None
    if not isinstance(comps, dict):
        return set()
    return {ref for ref, c in comps.items()
            if isinstance(c, dict) and c.get("assembly") in kinds}


def export_bom_csv(bom_json: Path, out_csv: Path, exclude: "set[str] | None" = None) -> Step:
    """The BOM as a CSV, grouped by identical part.

    Grouped because that is what a fab quotes: ten identical 100nF capacitors
    are one line item with a quantity, and a per-refdes list makes an assembler
    do that grouping by hand — badly.
    """
    try:
        rows = json.loads(Path(bom_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Step("bom", False, f"could not read {bom_json.name}: {exc}")

    if isinstance(rows, dict):
        rows = rows.get("components") or rows.get("rows") or []

    grouped: dict[tuple, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ref = str(row.get("refdes") or row.get("local_id") or "").strip()
        if ref and ref in (exclude or set()):
            continue  # DNP or footprint-only: not bought (project.yaml assembly)
        mpn = str(row.get("mpn") or row.get("part_hint") or "").strip()
        value = str(row.get("value") or row.get("description") or "").strip()
        footprint = str(row.get("footprint") or row.get("package_hint") or "").strip()
        key = (mpn, value, footprint)
        entry = grouped.setdefault(
            key,
            {
                "Designators": [],
                "Quantity": 0,
                "Value": value,
                "Footprint": footprint,
                "MPN": mpn,
                "Manufacturer": str(row.get("manufacturer") or row.get("manufacturer_hint") or ""),
            },
        )
        if ref:
            entry["Designators"].append(ref)
        entry["Quantity"] += 1

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["Designators", "Quantity", "Value", "Footprint", "MPN", "Manufacturer"]
        )
        writer.writeheader()
        for entry in sorted(grouped.values(), key=lambda e: (e["MPN"], e["Value"])):
            entry = dict(entry)
            entry["Designators"] = ",".join(sorted(entry["Designators"]))
            writer.writerow(entry)

    missing = sum(1 for k in grouped if not k[0])
    detail = ""
    if missing:
        # A line with no MPN is a line the fab has to ask about, which is a
        # round-trip on the quote — worth saying before the package is sent.
        detail = f"{missing} of {len(grouped)} line items have no MPN; the fab will have to ask"
    return Step("bom", True, detail, [out_csv.name])


def export_assembly(bom_csv: Path, out_dir: Path, *, lcsc: bool = False,
                    footprint_roots: list[Path] | None = None,
                    hand_fit: "set[str] | None" = None) -> list[Step]:
    """Per-house assembly uploads, beside the generic BOM.

    The grouped CSV above is what a *human* reads. Neither JLCPCB nor PCBWay
    accepts it: they want their own columns, and they differ from each other
    because they source differently — JLCPCB orders by LCSC part number, PCBWay
    turnkey by MPN. Emitting one file and labelling it for both would produce a
    quote against the wrong parts.

    Every warning is carried onto the step rather than swallowed. A package that
    silently ships an unbuildable BOM is exactly the failure this whole gate
    exists to prevent.
    """
    from ..agent.tools.bom import HOUSES, build_assembly
    from .stage6_compile_kicad import _DEFAULT_FOOTPRINTS

    positions = out_dir / "placement" / "positions.csv"
    steps: list[Step] = []
    for house in HOUSES:
        pkg = build_assembly(
            bom_csv,
            positions if positions.is_file() else None,
            out_dir / "assembly",
            house=house,
            # The board's own libraries first: its project and vendor
            # footprints are not in the stock set, and looked up there alone
            # 28 of the ear tag's lines had no SMD/THT type.
            footprint_roots=[*(footprint_roots or []), _DEFAULT_FOOTPRINTS],
            lcsc=lcsc,
            hand_fit=hand_fit,
        )
        detail = pkg.reason or "; ".join(pkg.warnings)
        if not lcsc:
            # The app's own words for it: there is a box on the Release tab.
            detail = detail.replace("Re-run with --lcsc to look them up",
                                    "Tick \"Look up LCSC part numbers\" on the Release tab and build again")
        steps.append(
            Step(
                f"assembly-{house}",
                pkg.ok,
                detail,
                [str(Path(f.path).relative_to(out_dir)) for f in pkg.files],
            )
        )
    return steps


def copy_native_project(kicad_dir: Path | None, out_dir: Path) -> Step:
    """The KiCad project itself, which is what a fab with KiCad would rather have.

    Gerbers are a lossy render of a board; the native project is the board.
    Several houses (PCBWay among them) accept it directly, and it costs nothing
    to include. It is the project Stage 6 publishes (publish.py): the board's
    files with ``libs/`` and the two library tables beside them, so it opens
    with every symbol and footprint on another machine. This used to look in
    the project root, where nothing has ever been written.
    """
    native = out_dir / "kicad"
    copied: list[str] = []
    if kicad_dir is not None and Path(kicad_dir).is_dir():
        for src in sorted(Path(kicad_dir).iterdir()):
            if src.is_file() and (src.suffix in (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_prl")
                                  or src.name in ("sym-lib-table", "fp-lib-table")):
                native.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, native / src.name)
                copied.append(src.name)
        libs = Path(kicad_dir) / "libs"
        if libs.is_dir() and copied:
            shutil.copytree(libs, native / "libs", dirs_exist_ok=True)
            copied.append("libs/")
    if not copied:
        return Step("native", False, "no published KiCad project for this board yet: run the pipeline "
                                     "through Stage 6 (it publishes to kicad/)")
    return Step("native", True, "", copied)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def run_gate(pipeline_dir: Path, out_dir: Path, board: str | None = None) -> dict:
    """kicad-happy's fabrication gate, over the analyzer JSON Stage 8 wrote.

    The gate reads analyzer output rather than the board, so it can only answer
    for what has actually been analyzed. A missing analysis is reported as
    unknown, never as a pass — "we did not check" and "it is fine" are the two
    answers that must never be confused here.
    """
    from ..agent.kicad_happy import find_kicad_happy

    base = find_kicad_happy()
    if base is None:
        return {"ok": False, "skipped": True, "reason": "kicad-happy is not available"}

    # Where Stage 8 writes it: review/, or review.<board>/ on a multi-board project.
    review = Path(pipeline_dir) / ("review" if board is None else f"review.{board}")
    sch = review / "schematic.json"
    pcb = review / "pcb.json"
    missing = [p.name for p in (sch, pcb) if not p.is_file()]
    if missing:
        return {
            "ok": False,
            "skipped": True,
            "reason": (
                f"no analyzer output ({', '.join(missing)}): run the pipeline through Stage 8 "
                "(Review) first. Without it the gate cannot tell a clean board from an "
                "unexamined one"
            ),
        }

    script = base / "skills" / "kicad" / "scripts" / "fab_release_gate.py"
    out_json = out_dir / "fab_gate.json"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--schematic", str(sch), "--pcb", str(pcb),
             "--output", str(out_json)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            cwd=str(script.parent),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "skipped": True, "reason": f"could not run the gate: {exc}"}

    if out_json.is_file():
        try:
            gate = json.loads(out_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            gate = None
        if isinstance(gate, dict):
            gate.setdefault("ok", _gate_passed(gate))
            gate["skipped"] = False
            return gate

    return {
        "ok": False,
        "skipped": True,
        "reason": (proc.stderr or proc.stdout or "the gate produced no readable result").strip()[:400],
    }


def _gate_passed(gate: dict) -> bool:
    """Read the gate's own verdict, defaulting to 'not passed'.

    The default matters: a gate whose shape we failed to understand must not be
    read as approval.
    """
    verdict = str(gate.get("status") or gate.get("verdict") or "").lower()
    if verdict in ("pass", "passed", "ready", "go"):
        return True
    if verdict:
        return False
    checks = gate.get("checks") or []
    if not isinstance(checks, list) or not checks:
        return False
    return not any(str(c.get("status", "")).lower() == "fail" for c in checks if isinstance(c, dict))


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def export_impedance(pipeline: Path, pcb: Path | None, out_dir: Path,
                     hdm: Path | None = None) -> Step:
    """The fab's table of impedance-controlled traces, from the HDM the board came from.

    A fab cannot see which traces are meant to be 50 ohms: on the gerbers they
    are just widths. Without this sheet the controlled lines are etched like
    any other, and the board measures whatever it measures.

    Not ok when a controlled class has no width from a stackup. That board is
    not quotable: the fab would be asked to control an impedance on a trace
    drawn at the Default width, which is the one thing the sheet exists to stop.
    """
    import yaml

    from . import impedance

    candidates = ([hdm] if hdm is not None else []) + (
        [pipeline / f"hdm.{pcb.stem}.yaml"] if pcb is not None else []) + [pipeline / "hdm.yaml"]
    hdm_path = next((c for c in candidates if c.is_file()), None)
    if hdm_path is None:
        if pcb is not None and pcb.is_file():
            # A board with no HDM beside it cannot say which of its traces are
            # controlled, and "nothing found" is not "nothing to control": a
            # package released that way would drop every impedance requirement
            # the board was designed with (Codex, PR #31).
            return Step("impedance", False,
                        f"no hdm.yaml for {pcb.name}: cannot tell which traces are "
                        "impedance-controlled; run the pipeline through Stage 5 for this board")
        return Step("impedance", True, "no board and no hdm.yaml; nothing to say about impedance")
    # A hand-edited HDM that does not parse is a refused step, not a crashed
    # build: the package and its refusal notice are still owed (Codex, PR #31).
    try:
        hdm = yaml.safe_load(hdm_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return Step("impedance", False, f"could not read {hdm_path.name}: {exc}")
    if not isinstance(hdm, dict):
        return Step("impedance", False, f"{hdm_path.name} is not a mapping")
    # Review first. A tag that matched nothing leaves no controlled net and so
    # no table, and returning "nothing to control" before asking would let the
    # requirement it was meant to carry vanish from a quotable package.
    # A malformed value in a hand-edited HDM refuses the step; it must not
    # abort the build and lose the refusal notice (Codex, PR #31).
    try:
        errors = sorted({f["rule_id"] for f in impedance.review(hdm) if f["severity"] == "error"})
        table = impedance.fab_table(hdm)
    except (impedance.ImpedanceConfigError, TypeError, ValueError, AttributeError) as exc:
        return Step("impedance", False, f"{hdm_path.name} has impedance settings that do not parse: {exc}")
    if table is None:
        if errors:
            return Step("impedance", False,
                        f"controlled impedance is not ready for a fab ({', '.join(errors)}); "
                        "see Stage 8's ZC findings")
        return Step("impedance", True, "no impedance-controlled nets")
    dest = out_dir / "fab" / "impedance.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(table, encoding="utf-8")
    # Any impedance error Stage 8 would report blocks the package, not only a
    # missing number: two complete classes for one pair (ZC-005) fill every
    # cell of the sheet and still ask the fab for two geometries at once. The
    # design is judged before the routing: a board cannot be checked against
    # a sheet that is itself incomplete or contradictory.
    if "NOT SET" in table or errors:
        return Step(
            "impedance", False,
            "controlled impedance is not ready for a fab"
            + (f" ({', '.join(errors)})" if errors else "")
            + "; see fab/impedance.md and Stage 8's ZC findings",
            [str(dest.relative_to(out_dir))],
        )
    # Then what was routed: every controlled net is, at its width, on its
    # layer, and a coplanar line with its ground copper at its gap.
    if pcb is not None and pcb.is_file():
        from blpl.emitter import sexpr

        try:
            board = sexpr.parse(pcb.read_text(encoding="utf-8"))
            problems = (
                [f"{n} is controlled in the HDM but has no routed copper on the board"
                 for n in impedance.unrouted_controlled(hdm, board)]
                + [f"{n} on {lay}" for n, lay, _ in impedance.layer_violations(hdm, board)]
                + [f"{n} at {w:g} mm, not {want:g}"
                   for n, w, want, _ in impedance.width_violations(hdm, board)]
                + [f"{cls} has no ground zone on {lay}"
                   for cls, lay in impedance.coplanar_without_ground(hdm, board)]
                + impedance.coplanar_gap_problems(hdm, board)
                + impedance.pair_problems(hdm, board)
                + [(f"{cls} on {lay} has no reference layer at all" if ref == "none"
                    else f"{cls} on {lay} has no ground or power plane on {ref} under it")
                   for cls, lay, ref in impedance.missing_reference(hdm, board)]
                + impedance.stackup_mismatch(hdm, board)
            )
        except Exception as exc:  # an unreadable board cannot be vouched for
            return Step("impedance", False, f"could not read {pcb.name} to check controlled routing: {exc}",
                        [str(dest.relative_to(out_dir))])
        if problems:
            shown = ", ".join(problems[:6])
            more = f" and {len(problems) - 6} more" if len(problems) > 6 else ""
            return Step(
                "impedance", False,
                f"the routed board does not build the controlled impedances: {shown}{more}",
                [str(dest.relative_to(out_dir))],
            )
    return Step("impedance", True, "", [str(dest.relative_to(out_dir))])


def board_files(project_dir: Path, board: str | None = None) -> dict:
    """Where this board's released files come from, as the pipeline leaves them.

    The board, schematic and native project are the ones Stage 6 publishes,
    and the router republishes once it has routed: ``<board dir>/kicad/``.
    Nothing is ever written to the project root, which is where this used to
    look, so every release came out with no fabrication files at all. A board
    published nowhere falls back to its newest emit in ``.pipeline/``. The
    BOM, HDM and Stage 8 output carry the board in their names on a
    multi-board project, as every stage writes them.
    """
    from . import project_manifest, publish

    project_dir = Path(project_dir)
    man = project_manifest.discover(project_dir)
    if man.implicit:
        if board and board != man.boards[0].name:
            raise ValueError(f"{project_dir.name} is a single-board project; it has no board {board!r}")
        qual, name = None, man.boards[0].name
    else:
        names = [b.name for b in man.boards]
        if not board:
            raise ValueError(f"{project_dir.name} has more than one board ({', '.join(names)}); say which")
        if board not in names:
            raise ValueError(f"{project_dir.name} has no board {board!r} ({', '.join(names)})")
        qual = name = board
    pipeline = project_manifest.pipeline_dir(project_dir)
    kicad_dir = project_manifest.board_dir(project_dir, man, name) / publish.PUBLISH_DIRNAME
    pcbs = sorted(kicad_dir.glob("*.kicad_pcb")) if kicad_dir.is_dir() else []
    pcb = pcbs[0] if pcbs else None
    if pcb is None:
        from .cli import _newest_emitted

        pcb = _newest_emitted(pipeline, ".kicad_pcb", qual)
    art = lambda stem, suffix="json": project_manifest.artifact_path(project_dir, stem, board=qual, suffix=suffix)
    bom = art("bom_resolved") if art("bom_resolved").is_file() else art("bom")
    return {"board": qual, "pipeline": pipeline, "pcb": pcb,
            "kicad_dir": kicad_dir if kicad_dir.is_dir() else None,
            "bom": bom, "hdm": art("hdm", "yaml")}


def build(
    project_dir: Path,
    *,
    now: str = "",
    kicad_cli: str | None = None,
    lcsc: bool = False,
    odb: bool = False,
    circuithub: bool = False,
    board: str | None = None,
) -> dict:
    """Build the release package. Returns the manifest.

    The zip is written even when the gate refuses, carrying a README that says
    so — a rejected board is exactly when its gerbers are worth looking at, and
    a package that cannot be opened teaches nobody anything. What must never
    happen is a rejected board packaged so it looks accepted.
    """
    project_dir = Path(project_dir)
    src = board_files(project_dir, board)
    pipeline = src["pipeline"]
    qual = src["board"]
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
    name = _UNSAFE.sub("-", project_dir.name + (f"-{qual}" if qual else "")) or "board"

    release_root = release_dir(project_dir, qual)
    out_dir = release_root / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    steps: list[Step] = []
    pcb = src["pcb"]
    if pcb is not None:
        steps.extend(export_fabrication(pcb, out_dir, kicad_cli=kicad_cli,
                                        fab=_fabrication(src["hdm"])))
        if odb:
            steps.append(export_odb(pcb, out_dir, name, kicad_cli=kicad_cli))
    else:
        steps.append(Step("fabrication", False, "no board for this project yet: run the pipeline "
                                                "through Stage 6 (Build PCB), and Stage 6a to route it"))
        if odb:
            steps.append(Step("odb", False, "no board to export as ODB++"))

    bom_json = src["bom"]
    bom_csv = out_dir / "bom" / f"{name}-bom.csv"
    hand_fit = _hand_fit(pipeline, src["hdm"])
    if bom_json.is_file():
        steps.append(export_bom_csv(bom_json, bom_csv, exclude=_not_bought(pipeline, src["hdm"])))
        libs = src["kicad_dir"] / "libs" if src["kicad_dir"] is not None else None
        steps.extend(export_assembly(bom_csv, out_dir, lcsc=lcsc,
                                     footprint_roots=[libs] if libs is not None and libs.is_dir() else None,
                                     hand_fit=hand_fit))
    else:
        steps.append(Step("bom", False, "no BOM yet: run the pipeline through Stage 1"))

    steps.append(copy_native_project(src["kicad_dir"], out_dir))
    if circuithub:
        steps.append(export_circuithub(
            src["kicad_dir"], bom_json, out_dir, name,
            _assembled_as(pipeline, src["hdm"], ("dnp", "footprint_only", "hand_fit"))))
    z_step = export_impedance(pipeline, pcb, out_dir, src["hdm"])
    steps.append(z_step)
    steps.append(write_board_specs(pcb, src["hdm"], out_dir, board=name,
                                   impedance_sheet=(out_dir / "fab" / "impedance.md").is_file()))

    gate = run_gate(pipeline, out_dir, qual)
    gate_ok = bool(gate.get("ok")) and not gate.get("skipped")
    quotable = gate_ok and z_step.ok

    if not quotable:
        (out_dir / "READ-ME-FIRST.txt").write_text(
            _refusal_text(gate, steps, gate_ok=gate_ok), encoding="utf-8")

    files: list[dict] = []
    for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
        files.append(
            {
                "path": str(path.relative_to(out_dir)),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )

    cli = kicad_cli or find_kicad_cli()
    manifest = {
        "release": name,
        "schema_version": 1,
        "created": stamp,
        "quotable": quotable,
        "gate": gate,
        "steps": [s.to_dict() for s in steps],
        "tools": {"kicad_cli": _tool_version(cli) if cli else "not found"},
        "files": files,
        "incomplete": [s.name for s in steps if not s.ok],
        # Which of the optional outputs this build was asked for, so the
        # download can say what is in it without anyone opening the zip.
        "options": {"lcsc": lcsc, "odb": odb, "circuithub": circuithub},
        # What the assembly files left for fitting by hand, so regenerating
        # them from this package (blpl bom-assembly) leaves out the same parts.
        "hand_fit": sorted(hand_fit),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    archive = release_root / f"{name}-{stamp}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
            zf.write(path, path.relative_to(out_dir))
    manifest["archive"] = str(archive)

    # The stable name the app serves, so a download link does not need to know
    # the timestamp of the newest run.
    latest = release_root / "latest.zip"
    shutil.copy2(archive, latest)
    (release_root / "latest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def release_dir(project_dir: Path, board: str | None = None) -> Path:
    """Where a board's releases go: ``release/``, or ``release/<board>/`` when
    the project has more than one, so two boards' packages never overwrite
    each other's ``latest``."""
    root = Path(project_dir) / "release"
    return root if board is None else root / board


def _refusal_text(gate: dict, steps: list[Step], *, gate_ok: bool = False) -> str:
    lines = [
        "THIS PACKAGE IS NOT CLEARED FOR FABRICATION.",
        "",
    ]
    if gate_ok:
        lines.append("The release gate passed, but the package is not complete (below).")
    elif gate.get("skipped"):
        lines.append(f"The release gate could not run: {gate.get('reason', 'no reason recorded')}")
        lines.append(
            "That is not the same as passing. Nothing here has been checked against the "
            "fabrication criteria."
        )
    else:
        lines.append("The release gate ran and did not pass.")
        for check in gate.get("checks", []):
            if isinstance(check, dict) and str(check.get("status", "")).lower() in ("fail", "warn"):
                lines.append(f"  [{check.get('status')}] {check.get('check_id')}: {check.get('message')}")

    failed = [s for s in steps if not s.ok]
    if failed:
        lines += ["", "Export steps that did not produce output:"]
        lines += [f"  {s.name}: {s.detail}" for s in failed]

    lines += [
        "",
        "The files are included because they are worth inspecting. They are not worth sending",
        "to a board house: a board opens and renders perfectly with nets left unrouted or checks",
        "failed, which is exactly what makes shipping one by accident easy.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="blpl-release")
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--board", default=None, help="Which board, on a multi-board project.")
    parser.add_argument(
        "--lcsc",
        action="store_true",
        help="Look up LCSC part numbers for the JLCPCB BOM (needs network).",
    )
    parser.add_argument(
        "--odb",
        action="store_true",
        help="Also export the board as an ODB++ job, zipped inside the package.",
    )
    parser.add_argument(
        "--circuithub",
        action="store_true",
        help="Also write a CircuitHub design package (the native project and a BOM, zipped).",
    )
    args = parser.parse_args(argv)

    try:
        manifest = build(Path(args.project_dir), lcsc=args.lcsc, odb=args.odb,
                         circuithub=args.circuithub, board=args.board)
    except ValueError as exc:
        print(f"release: {exc}", file=sys.stderr, flush=True)
        return 2
    for step in manifest["steps"]:
        state = "ok" if step["ok"] else f"FAILED — {step['detail']}"
        print(f"  {step['step']}: {state}", flush=True)

    gate = manifest["gate"]
    if manifest["quotable"]:
        print(f"release: gate passed → {manifest['archive']}", flush=True)
        return 0
    if gate.get("ok") and not gate.get("skipped"):
        why = "incomplete: " + ", ".join(manifest["incomplete"])
    else:
        why = gate.get("reason") or "the gate did not pass"
    print(
        f"release: NOT cleared for fabrication ({why}). Package written anyway for inspection: "
        f"{manifest['archive']}",
        file=sys.stderr,
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
