"""Reading an existing KiCad design back into BLPL's data model.

Everything in the pipeline points one way — Markdown becomes a board — and that
made "start from an existing board" impossible. Not because the parsing was
hard: ``emitter/sexpr.py`` has read S-expressions since the first commit, but
only ever for *library* files. What was missing is a reader for design
documents, which is what makes the interesting things possible: importing an
open-hardware board, extracting a proven power section as a reusable module,
remixing several boards into a carrier.

The model here is deliberately the same shape the pipeline already speaks —
components with refdes and footprint, nets with (refdes, pin) members — so an
imported board and a generated one are the same kind of thing downstream.

What this reads and does not:

  reads   components (refdes, value, lib_id, footprint, MPN when a property
          carries it), the schematic's net names via labels and wire
          connectivity, and PCB footprint positions with their pad nets.
  skips   graphics, text, dimensions, and anything cosmetic. A module extracted
          from a board carries its electrical content; the silkscreen art of
          somebody else's board is not something to inherit.

Nets come from the PCB when one is present, because the PCB is where KiCad has
already resolved connectivity into per-pad net assignments — the schematic would
require re-deriving it from wire geometry, which is exactly the error-prone step
this can avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..emitter import sexpr


@dataclass
class ImportedComponent:
    refdes: str
    value: str = ""
    lib_id: str = ""
    footprint: str = ""
    mpn: str = ""
    datasheet: str = ""
    position: tuple[float, float] | None = None   # from the PCB, mm
    properties: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {
            "refdes": self.refdes,
            "value": self.value,
            "lib_id": self.lib_id,
            "footprint": self.footprint,
        }
        if self.mpn:
            out["mpn"] = self.mpn
        if self.datasheet:
            out["datasheet"] = self.datasheet
        if self.position:
            out["position"] = list(self.position)
        return out


@dataclass
class ImportedNet:
    name: str
    members: list[tuple[str, str]] = field(default_factory=list)  # (refdes, pad)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "members": [{"refdes": r, "pin": p} for r, p in self.members],
        }


@dataclass
class ImportedBoard:
    name: str
    components: dict[str, ImportedComponent] = field(default_factory=dict)
    nets: dict[str, ImportedNet] = field(default_factory=dict)
    source_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source_files": self.source_files,
            "components": [c.to_dict() for c in self.components.values()],
            "nets": [n.to_dict() for n in self.nets.values()],
            "warnings": self.warnings,
            "summary": {
                "components": len(self.components),
                "nets": len(self.nets),
                "unrouted_hint": sum(1 for n in self.nets.values() if len(n.members) < 2),
            },
        }


# Property names that carry a manufacturer part number. KiCad has no standard
# one, so every house style has to be recognised or imported boards arrive with
# no sourcing information at all.
_MPN_KEYS = ("mpn", "manufacturer part number", "part number", "pn", "manufacturer_part_number")


def _props(node: sexpr.Sexp) -> dict[str, str]:
    out: dict[str, str] = {}
    for prop in sexpr.find_all(node, "property"):
        items = [i for i in prop[1:] if isinstance(i, str)]
        if len(items) >= 2:
            out[sexpr.unquote(items[0])] = sexpr.unquote(items[1])
    return out


def _atom(node: sexpr.Sexp, tag: str) -> str:
    found = sexpr.find(node, tag)
    if not found:
        return ""
    for item in found[1:]:
        if isinstance(item, str):
            return sexpr.unquote(item)
    return ""


def read_schematic(path: Path, board: ImportedBoard) -> None:
    """Pull components out of a .kicad_sch (including its hierarchical sheets)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        doc = sexpr.parse(text)
    except sexpr.ParseError as exc:
        board.warnings.append(f"{Path(path).name}: could not parse ({exc})")
        return

    for sym in sexpr.find_all(doc, "symbol"):
        props = _props(sym)
        refdes = props.get("Reference", "")
        if not refdes or refdes.startswith("#"):
            # '#PWR' and friends are power-flag pseudo-symbols, not parts.
            continue
        lower = {k.lower(): v for k, v in props.items()}
        existing = board.components.get(refdes)
        component = existing or ImportedComponent(refdes=refdes)
        component.value = component.value or props.get("Value", "")
        component.lib_id = component.lib_id or _atom(sym, "lib_id")
        component.footprint = component.footprint or props.get("Footprint", "")
        component.datasheet = component.datasheet or props.get("Datasheet", "").strip()
        if not component.mpn:
            for key in _MPN_KEYS:
                if lower.get(key):
                    component.mpn = lower[key]
                    break
        component.properties.update(props)
        board.components[refdes] = component

    board.source_files.append(Path(path).name)


def read_pcb(path: Path, board: ImportedBoard) -> None:
    """Pull footprints, their positions, and per-pad net assignments from a .kicad_pcb.

    The PCB is the better source for connectivity: KiCad has already resolved it
    into a net number and name on every pad, so nothing here has to re-derive
    nets from wire geometry — the step most likely to get connectivity subtly
    wrong.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        doc = sexpr.parse(text)
    except sexpr.ParseError as exc:
        board.warnings.append(f"{Path(path).name}: could not parse ({exc})")
        return

    for fp in sexpr.find_all(doc, "footprint"):
        props = _props(fp)
        refdes = props.get("Reference", "")
        if not refdes or refdes.startswith("#"):
            continue
        component = board.components.get(refdes) or ImportedComponent(refdes=refdes)
        component.value = component.value or props.get("Value", "")
        lib_id = ""
        for item in fp[1:]:
            if isinstance(item, str):
                lib_id = sexpr.unquote(item)
                break
        component.footprint = component.footprint or lib_id
        at = sexpr.find(fp, "at")
        if at:
            coords = [i for i in at[1:] if isinstance(i, str)]
            if len(coords) >= 2:
                try:
                    component.position = (float(coords[0]), float(coords[1]))
                except ValueError:
                    pass
        lower = {k.lower(): v for k, v in props.items()}
        if not component.mpn:
            for key in _MPN_KEYS:
                if lower.get(key):
                    component.mpn = lower[key]
                    break
        component.properties.update(props)
        board.components[refdes] = component

        for pad in sexpr.find_all(fp, "pad"):
            pad_name = ""
            for item in pad[1:]:
                if isinstance(item, str):
                    pad_name = sexpr.unquote(item)
                    break
            net = sexpr.find(pad, "net")
            if not net:
                continue
            net_name = ""
            for item in net[1:]:
                if isinstance(item, str) and not item.lstrip("-").isdigit():
                    net_name = sexpr.unquote(item)
            if not net_name:
                continue
            entry = board.nets.setdefault(net_name, ImportedNet(name=net_name))
            member = (refdes, pad_name)
            if member not in entry.members:
                entry.members.append(member)

    board.source_files.append(Path(path).name)


def read_project(directory: Path, *, name: str = "") -> ImportedBoard:
    """Read every schematic and PCB in a directory into one board.

    Reading the schematic first and the PCB second is intentional: the schematic
    carries the richer properties (MPN, datasheet), the PCB carries the truth
    about connectivity and placement, and later reads only fill gaps.
    """
    directory = Path(directory)
    board = ImportedBoard(name=name or directory.name)

    for sch in sorted(directory.rglob("*.kicad_sch")):
        read_schematic(sch, board)
    for pcb in sorted(directory.rglob("*.kicad_pcb")):
        read_pcb(pcb, board)

    if not board.source_files:
        board.warnings.append(f"no .kicad_sch or .kicad_pcb found under {directory}")
    if board.components and not board.nets:
        # Worth saying out loud: a schematic-only import can list parts but
        # cannot support module extraction, which cuts on net boundaries.
        board.warnings.append(
            "no nets found — without a .kicad_pcb, connectivity is unknown and "
            "modules cannot be cut from this design"
        )
    return board
