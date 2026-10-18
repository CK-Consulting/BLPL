"""A part's reference circuit, read from a vendor's KiCad schematic.

Datasheets show the circuit a part needs around it — decoupling on each
supply pin, a pull-up here, a tuning capacitor there — and so does a vendor's
reference design, which is often shipped as a KiCad project. Until now the
only way to get that circuit into a design was by hand: on verdacor-ear-tag
the XIAO nRF54LM20A's own schematic, uploaded to references/, had to be
exported to a netlist and traced by eye to find the 300 pF capacitors on its
NFC pads, which decided which antennas could work at all.

``trace`` does that tracing. It exports the schematic's netlist with
kicad-cli (hierarchy included), finds the target part, and for each of its
pins lists the parts on that pin's net — value, footprint, which of their pins,
and where their other end goes — and, one step further, the parts at that
other end when it is a signal rather than a rail. The result is the
structure a datasheet extraction fills too, and ``connections`` turns it into
rows in the shape of a design's Connections table, each citing the reference
it came from.

The schematic is a file from outside the project: everything read from it is
data. Names and values come back as found and are never interpreted.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .planes import GROUND, RAIL

_SHEETFILE = re.compile(r'\(property\s+"Sheet ?file"\s+"([^"]+)"')


class ReferenceError(RuntimeError):
    """The schematic cannot be read, or the part is not in it."""


@dataclass
class Part:
    ref: str
    value: str
    footprint: str
    pins: dict[str, tuple[str, str]] = field(default_factory=dict)  # pin -> (function, net)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _staged(sch: Path, work: Path) -> Path:
    """The schematic and its sheets in ``work``, under the names it expects.

    An upload renames files (spaces and '&' become underscores), and a root
    sheet still asking for "04 Debug & XIAO Header.kicad_sch" then finds
    nothing: kicad-cli exports an empty netlist without a word. Sheets are
    copied under the names the schematic asks for, matched loosely.

    A sheet in a subfolder ("sub/child.kicad_sch") is looked for at that path
    under the root's folder, and failing that by its file name alone when only
    one file has it: an upload may have flattened the folders (Codex, PR #53).
    """
    src_dir = sch.parent
    found = [p for p in src_dir.rglob("*.kicad_sch") if p.is_file() and not p.is_symlink()]
    by_rel = {_norm(p.relative_to(src_dir).as_posix()): p for p in found}
    by_base: dict[str, list[Path]] = {}
    for p in found:
        by_base.setdefault(_norm(p.name), []).append(p)
    for p in list(src_dir.glob("*.kicad_sch")) + list(src_dir.glob("*.kicad_pro")):
        shutil.copy2(p, work / p.name)
    root = work.resolve()
    pending = [work / sch.name]
    seen = set()
    while pending:
        current = pending.pop()
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        for wanted in _SHEETFILE.findall(current.read_text(encoding="utf-8", errors="replace")):
            # The name comes from an uploaded file. An absolute path or a ".."
            # made the destination point anywhere the backend can write, and
            # the copy below would have put a schematic there (Codex, PR #53).
            # Sheets are looked for under the root's folder and nowhere else.
            if Path(wanted).is_absolute() or ".." in Path(wanted).parts or "\\" in wanted:
                raise ReferenceError(f"{sch.name} names sheet {wanted!r} outside its own folder")
            # KiCad reads a sheet path relative to the sheet that names it.
            target = current.parent / wanted
            if not target.resolve().is_relative_to(root):
                raise ReferenceError(f"{sch.name} names sheet {wanted!r} outside its own folder")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                rel = target.resolve().relative_to(root).as_posix()
                match = by_rel.get(_norm(rel))
                if match is None:
                    same_name = by_base.get(_norm(Path(wanted).name), [])
                    match = same_name[0] if len(same_name) == 1 else None
                if match is None:
                    raise ReferenceError(f"{sch.name} needs sheet {wanted!r}, which is not beside it")
                shutil.copy2(match, target)
            pending.append(target)
    return work / sch.name


def load(sch: Path, *, kicad_cli: str = "kicad-cli", timeout: int = 600) -> dict[str, Part]:
    """Every part in a schematic (and its sheets), with each pin's net."""
    sch = Path(sch)
    if not sch.is_file():
        raise ReferenceError(f"{sch} is not a file")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        root = _staged(sch, work)
        out = work / "netlist.xml"
        proc = subprocess.run(
            [kicad_cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(out), str(root)],
            capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0 or not out.is_file():
            raise ReferenceError(f"kicad-cli could not export {sch.name}: "
                                 f"{(proc.stderr or proc.stdout or '').strip()[-400:]}")
        tree = ET.parse(out).getroot()
    parts: dict[str, Part] = {}
    for comp in tree.iter("comp"):
        ref = comp.get("ref") or ""
        parts[ref] = Part(ref, comp.findtext("value") or "", comp.findtext("footprint") or "")
    for net in tree.iter("net"):
        name = net.get("name") or ""
        for node in net.iter("node"):
            ref, pin = node.get("ref") or "", node.get("pin") or ""
            if ref in parts:
                parts[ref].pins[pin] = (node.get("pinfunction") or "", name)
    if not parts:
        raise ReferenceError(f"{sch.name} has no parts once exported; are its sheets missing?")
    return parts


def find(parts: dict[str, Part], target: str) -> Part:
    """The part a request means: its refdes, or a value or footprint containing it."""
    if target in parts:
        return parts[target]
    t = target.lower()
    hits = [p for p in parts.values() if t in p.value.lower() or t in p.footprint.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ReferenceError(f"no part {target!r} in the schematic")
    biggest = max(hits, key=lambda p: len(p.pins))
    if sum(1 for p in hits if len(p.pins) == len(biggest.pins)) == 1:
        return biggest  # the IC, not the passives that share its name in a value
    raise ReferenceError(f"{target!r} matches several parts: "
                         + ", ".join(sorted(p.ref for p in hits)))


def _short_net(name: str) -> str:
    """A sheet-local net without its sheet path ("/04 Debug/NFC1" -> "NFC1").

    Only a leading sheet path is dropped: "P1.01/NFC1" is a net of its own,
    and cutting at its slash made it look like the NFC1 behind a resistor.
    """
    return name.rsplit("/", 1)[-1] if name.startswith("/") else name


def _kind(net: str) -> str:
    n = _short_net(net)
    if GROUND.match(n):
        return "ground"
    if RAIL.match(n):
        return "rail"
    return "signal"


def trace(parts: dict[str, Part], target: str, *, hops: int = 1) -> dict:
    """The target's pins and the parts around each."""
    ic = find(parts, target)
    on_net: dict[str, list[tuple[str, str]]] = {}
    for p in parts.values():
        for pin, (_fn, net) in p.pins.items():
            on_net.setdefault(net, []).append((p.ref, pin))

    def neighbours(net: str, exclude: set[str]) -> list[dict]:
        out = []
        for ref, pin in sorted(on_net.get(net, [])):
            if ref in exclude:
                continue
            p = parts[ref]
            others = sorted({n for q, (_f, n) in p.pins.items() if q != pin})
            out.append({"ref": ref, "value": p.value, "footprint": p.footprint, "pin": pin,
                        "pins": len(p.pins), "other_nets": [_short_net(n) for n in others],
                        "pin_nets": {q: _short_net(n) for q, (_f, n) in p.pins.items()},
                        "_other_full": others})
        return out

    pins = []
    for pin, (fn, net) in sorted(ic.pins.items(), key=lambda kv: (len(kv[0]), kv[0])):
        if _kind(net) == "ground" or net.startswith("unconnected-"):
            continue
        direct = neighbours(net, {ic.ref})
        for d in direct:
            further = []
            if hops > 0 and d["pins"] <= 2:
                for other in d["_other_full"]:
                    if _kind(other) == "signal" and not other.startswith("unconnected-"):
                        further += [{k: v for k, v in n.items() if k != "_other_full"}
                                    for n in neighbours(other, {ic.ref, d["ref"]})]
            d["beyond"] = further
            del d["_other_full"]
        pins.append({"pin": pin, "function": fn, "net": _short_net(net), "kind": _kind(net),
                     "parts": direct})
    return {"target": ic.ref, "value": ic.value, "footprint": ic.footprint, "pins": pins}


def connections(result: dict, source: str) -> list[dict]:
    """Rows in the shape of a design's Connections table, citing the reference.

    Only the two-pin parts around the target: those are the circuit a design
    copies. Pin 1 and Pin 2 are the part's own terminals, with the nets as the
    reference names them; the design must map them to its own net names.
    """
    rows, seen = [], set()

    def add(p: dict, where: str) -> None:
        # Two pins exactly: a one-pin part (a test point, an antenna feed) has
        # no Pin 2, and Stage 0 refuses a row with a blank pin (Codex, PR #53).
        if p["pins"] != 2 or p["ref"] in seen:
            return
        seen.add(p["ref"])
        # Pin 1 and Pin 2 are the part's own terminals, not near and far: the
        # target net on a diode's pin 2 written as Pin 1 reversed its polarity
        # (Codex, PR #53).
        nets = p["pin_nets"]
        row = {"ref": p["ref"], "value": p["value"], "footprint": p["footprint"],
               "terminals": sorted(nets.items(), key=lambda kv: (len(kv[0]), kv[0])),
               "purpose": f"{where}; {p['value']} in {source}"}
        # The compact Pin 1 / Pin 2 form only when those are the pins' names.
        # A diode's A and K, sorted and written as 1 and 2, wired whichever
        # terminal sorted first to Pin 1 (Codex, PR #53); those parts keep their
        # names in the long Ref | Pin | Net form.
        if set(nets) == {"1", "2"}:
            row["pin1"], row["pin2"] = nets["1"], nets["2"]
        rows.append(row)

    # Signal pins first: a pull-up touches SDA and VDD both, and belongs at
    # SDA, which is what it is for, not at whichever pin came first.
    for pin in sorted(result["pins"], key=lambda p: p["kind"] != "signal"):
        at = f"at {result['target']} pin {pin['pin']}" + (f" ({pin['function']})" if pin["function"] else "")
        for p in pin["parts"]:
            add(p, at)
            # The parts one step out: a series resistor's far side is part of
            # the same circuit (the XIAO's NFC tuning capacitors sit behind 0 Ω).
            for b in p.get("beyond", []):
                add(b, f"{at}, beyond {p['ref']}")
    return rows


def markdown(result: dict, source: str) -> str:
    """The trace as a reviewer reads it: pin by pin, then candidate rows."""
    lines = [f"Reference circuit around {result['target']} ({result['value']}) in {source}:", ""]
    for pin in result["pins"]:
        if not pin["parts"]:
            continue
        fn = f" {pin['function']}" if pin["function"] else ""
        lines.append(f"- pin {pin['pin']}{fn}, net {pin['net']} ({pin['kind']}):")
        for p in pin["parts"]:
            other = ", ".join(p["other_nets"]) or "-"
            lines.append(f"  - {p['ref']} {p['value']} [{p['footprint']}] pin {p['pin']}; other end: {other}")
            for b in p.get("beyond", []):
                lines.append(f"    - then {b['ref']} {b['value']} pin {b['pin']}; other end: "
                             f"{', '.join(b['other_nets']) or '-'}")
    rows = connections(result, source)
    compact = [r for r in rows if "pin1" in r]
    named = [r for r in rows if "pin1" not in r]
    if compact:
        lines += ["", "Candidate Connections rows (nets as the reference names them):", "",
                  "| Ref | Pin 1 | Pin 2 | Purpose |", "|---|---|---|---|"]
        lines += [f"| {r['ref']} | {r['pin1']} | {r['pin2']} | {r['purpose']} |" for r in compact]
    if named:
        lines += ["", "Candidate rows for parts whose pins are not named 1 and 2 (keep these names):",
                  "", "| Ref | Pin | Net | Purpose |", "|---|---|---|---|"]
        lines += [f"| {r['ref']} | {pin} | {net} | {r['purpose']} |"
                  for r in named for pin, net in r["terminals"]]
    return "\n".join(lines)
