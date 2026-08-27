"""`blpl init` — build project.yaml from the design markdown you already wrote.

Stage 5 needs board dimensions, a stackup, and net-class electrical values. None
of those are parsed from Markdown, so it halts and dumps a `project.yaml.template`
for you to fill in by hand.

The frustrating part is that people *do* write all of it. dev.04's overview has a
"Project identity" table, a "Stackup" paragraph, and a "Net classes" table with
five fully-specified classes — and Stage 0 discards every one of them, because its
table classifier only recognises BOM and pinout shapes. So the pipeline throws
your config away and then halts asking you to re-type it somewhere else.

This module reads what you already wrote. It is deliberately conservative: it
fills in what it can prove, reports what it inferred and what it defaulted, and
never silently invents a number you have to trust. Anything it could not find is
left at a documented default and called out, so you know exactly which values are
yours and which are ours.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import markdown_tables as _md

# "110mm × 70mm", "110 mm x 70 mm", "110x70mm" — people write all of these.
_DIMENSIONS_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*[x×]\s*(\d+(?:\.\d+)?)\s*(?:mm)?", re.IGNORECASE
)
_LAYERS_RE = re.compile(r"(\d+)[- ]layer", re.IGNORECASE)
_THICKNESS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mm", re.IGNORECASE)
_FINISH_RE = re.compile(r"\b(ENIG|HASL|OSP|Immersion\s+\w+|Hard\s+Gold)\b", re.IGNORECASE)

_DEFAULT_NET_CLASS = {
    "trace_width": 0.2,
    "clearance": 0.15,
    "via_dia": 0.6,
    "via_drill": 0.3,
}


@dataclass
class InitResult:
    config: dict
    found: list[str] = field(default_factory=list)      # taken from your markdown
    defaulted: list[str] = field(default_factory=list)  # we had to pick


def _cell(row: dict[str, str], *names: str) -> str:
    # Headers are normalized to spaces before matching, because a design doc
    # writes `via_dia` where this searches for "via dia" — and the miss was
    # silent: on a real board every via dimension quietly became the default
    # (0.6/0.3 emitted where the table said 0.8/0.4), while trace_width only
    # survived by the accident of "width" being a substring of it. Board rules
    # are the one place a silent default is copper.
    lower = {k.lower().strip().replace("_", " ").replace("-", " "): v for k, v in row.items()}
    for n in names:
        for k, v in lower.items():
            if n in k:
                return v.strip()
    return ""


def _heading_for(text: str, line_start: int) -> str:
    lines = text.splitlines()
    for i in range(min(line_start, len(lines)) - 1, -1, -1):
        if lines[i].lstrip().startswith("#"):
            return lines[i].lstrip("# ").strip().lower()
    return ""


def _section_text(text: str, heading_contains: str) -> str:
    """Return the prose under the first heading matching a keyword."""
    lines = text.splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        if line.lstrip().startswith("#"):
            if capturing:
                break
            capturing = heading_contains in line.lower()
            continue
        if capturing:
            out.append(line)
    return "\n".join(out)


def _parse_number(value: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", value)
    return float(m.group(0)) if m else None


def build_config(project_dir: Path, *, board: str | None = None) -> InitResult:
    """Harvest project config from the project's Markdown.

    On a multi-board project the identity, stackup and net-class tables for a
    board live in that board's directory, and this read only the project root —
    so `init --board sb-ant` harvested nothing of sb-ant and wrote a project.yaml
    of defaults, which Stage 5 then accepted for the generated board.

    Root first, then the board's own files, so a board overrides what the project
    states in general and inherits what it does not restate. The file is still
    written at the project root, because that is where Stage 5 reads it.
    """
    project_dir = Path(project_dir)
    name = project_dir.name
    board_id = f"{name.upper()}-V1"
    dimensions = [100.0, 80.0]
    stackup = {"layers": 4, "thickness": 1.6, "finish": "ENIG"}
    net_classes: dict[str, dict] = {}

    found: list[str] = []
    defaulted: list[str] = []

    sources = sorted(project_dir.glob("*.md"))
    if board is not None:
        from . import project_manifest

        man = project_manifest.discover(project_dir)
        sources += sorted(project_manifest.board_dir(project_dir, man, board).glob("*.md"))

    for md_path in sources:
        text = md_path.read_text(encoding="utf-8")

        # --- identity + net classes come from tables Stage 0 discards ---
        for table in _md.extract_tables(text, md_path.name):
            heading = _heading_for(text, table.line_start)

            if "identity" in heading or "project" in heading:
                for row in table.rows:
                    key = _cell(row, "field").lower()
                    val = _cell(row, "value")
                    if not val:
                        continue
                    if key.startswith("name"):
                        name = val
                        found.append(f"name = {val}")
                    elif "board id" in key or key == "id":
                        board_id = val
                        found.append(f"board_id = {val}")
                    elif "dimension" in key or "size" in key:
                        m = _DIMENSIONS_RE.search(val)
                        if m:
                            dimensions = [float(m.group(1)), float(m.group(2))]
                            found.append(f"dimensions = {dimensions[0]} x {dimensions[1]} mm")

            elif "net class" in heading:
                for row in table.rows:
                    cls = _cell(row, "class")
                    if not cls:
                        continue
                    tw = _parse_number(_cell(row, "trace width", "width"))
                    cl = _parse_number(_cell(row, "clearance"))
                    vd = _parse_number(_cell(row, "via dia"))
                    vdr = _parse_number(_cell(row, "via drill"))
                    net_classes[cls] = {
                        "trace_width": tw if tw is not None else _DEFAULT_NET_CLASS["trace_width"],
                        "clearance": cl if cl is not None else _DEFAULT_NET_CLASS["clearance"],
                        "via_dia": vd if vd is not None else _DEFAULT_NET_CLASS["via_dia"],
                        "via_drill": vdr if vdr is not None else _DEFAULT_NET_CLASS["via_drill"],
                    }
                    # A class row that parsed PARTIALLY is the dangerous case:
                    # the class name is recognized, so nothing looked wrong,
                    # while individual fields fell to defaults without a word.
                    # Say which, per class, in the same place the other
                    # defaults are reported.
                    for field_name, got in (
                        ("trace_width", tw), ("clearance", cl),
                        ("via_dia", vd), ("via_drill", vdr),
                    ):
                        if got is None:
                            defaulted.append(
                                f"net class {cls}: {field_name} = "
                                f"{_DEFAULT_NET_CLASS[field_name]} (not found in its row)"
                            )
                if net_classes:
                    found.append(f"net_classes = {', '.join(net_classes)}")

        # --- stackup is usually prose, not a table ("4-layer, 1.6mm, ENIG") ---
        section = _section_text(text, "stackup")
        if section.strip():
            layers = _LAYERS_RE.search(section)
            thickness = _THICKNESS_RE.search(section)
            finish = _FINISH_RE.search(section)
            if layers:
                stackup["layers"] = int(layers.group(1))
            if thickness:
                stackup["thickness"] = float(thickness.group(1))
            if finish:
                stackup["finish"] = finish.group(1).upper()
            if layers or thickness or finish:
                found.append(
                    f"stackup = {stackup['layers']}-layer, "
                    f"{stackup['thickness']}mm, {stackup['finish']}"
                )

    # Stage 4 assigns every unmatched net to "Default", so it must exist or the
    # emitted .kicad_pro references a class that isn't there.
    if "Default" not in net_classes:
        net_classes["Default"] = dict(_DEFAULT_NET_CLASS)
        defaulted.append("net_classes.Default (not declared in your markdown)")

    if dimensions == [100.0, 80.0] and not any(f.startswith("dimensions") for f in found):
        defaulted.append("dimensions = 100 x 80 mm")
    if not any(f.startswith("stackup") for f in found):
        defaulted.append(f"stackup = 4-layer, 1.6mm, ENIG")
    if not any(f.startswith("board_id") for f in found):
        defaulted.append(f"board_id = {board_id}")

    width, height = dimensions
    config = {
        "project": {
            "name": name,
            "board_id": board_id,
            "dimensions": dimensions,
            "stackup": stackup,
        },
        "net_classes": net_classes,
        "boundaries": {
            "board_outline": {
                "type": "rect",
                "start": [0, 0],
                "end": [width, height],
                "layer": "Edge.Cuts",
                "width": 0.1,
            },
            "keepouts": [],
            "copper_zones": [],
        },
    }
    return InitResult(config=config, found=found, defaulted=defaulted)


_LIBRARIES_README = """\
# Project symbol & footprint libraries

Put symbols and footprints you drew **yourself** in here. This directory is searched
**first** — ahead of the Stage 3 auto-generated symbols and ahead of KiCad's stock
libraries — so anything you hand-author always wins over a guess.

    libraries/
      symbols/      <Library>.kicad_symdir/<Name>.kicad_sym   (or a flat <Library>.kicad_sym)
      footprints/   <Library>.pretty/<Name>.kicad_mod

## Why you're reading this

Stage 1 asks an LLM for a KiCad symbol name, and it will invent plausible ones that
do not exist (`Sensor_Motion:ICM-42670-P`, `RF_GPS:LC76G`). Those components get a
**generic placeholder** so the board still opens — but the placeholder is *not the
part*, and the board must not be fabricated until it's replaced.

Check `.pipeline/manual_library_work.md` after every run: it lists exactly which
symbols are missing and what to name them. Draw them, drop them in `symbols/`, and
re-run `blpl stage5 && blpl stage6` — the placeholder disappears on its own.
"""


def _ensure_custom_library_dirs(project_dir: Path) -> None:
    """Create the place where hand-authored symbols live, with a README saying why."""
    libs = Path(project_dir) / "libraries"
    (libs / "symbols").mkdir(parents=True, exist_ok=True)
    (libs / "footprints").mkdir(parents=True, exist_ok=True)
    readme = libs / "README.md"
    if not readme.exists():
        readme.write_text(_LIBRARIES_README, encoding="utf-8")


def write_config(
    project_dir: Path, *, force: bool = False, board: str | None = None
) -> tuple[Path, InitResult]:
    """Write project.yaml. Refuses to clobber an existing one unless forced."""
    project_dir = Path(project_dir)
    target = project_dir / "project.yaml"
    if target.exists() and not force:
        raise FileExistsError(
            f"{target} already exists — pass --force to overwrite it."
        )

    _ensure_custom_library_dirs(project_dir)

    result = build_config(project_dir, board=board)
    header = (
        "# Generated by `blpl init` from this project's design markdown.\n"
        "# Values marked (default) below were not found in your markdown — check them.\n"
        "# Re-run with --force to regenerate after editing the markdown.\n\n"
    )
    target.write_text(
        header + yaml.safe_dump(result.config, sort_keys=False), encoding="utf-8"
    )
    return target, result
