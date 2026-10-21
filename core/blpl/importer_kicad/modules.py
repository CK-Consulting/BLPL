"""Function-set modules: a proven block of a board, made reusable.

The driving case is carrier boards. A new design is rarely new all the way
down — it is a battery-charging section that already works, a radio front end
whose matching network took three spins to get right, an MCU block with its
decoupling, arranged onto a fresh carrier. Today all of that gets re-derived
from scratch every time, and the third spin's hard-won details get lost.

A module is a directory, because a directory is the thing a person can read,
review in a diff, and commit:

    modules/<name>/
      module.yaml          the contract: what it is, where it came from, and
                           crucially its *interface* — the nets that cross the
                           boundary, which is what makes it composable
      <SourceLib>.kicad_sym    every symbol the block uses, copied in
      <SourceLib>.pretty/      every footprint, copied in
      bom.json             the parts, with MPNs where the source knew them,
                           and each part's pins on the module's nets

The libraries keep the names they had on the source board rather than being
merged into one. That is what lets the module directory serve directly as a
library root: a ``lib_id`` carried over from the source board still resolves,
with no rewriting step to get wrong.

Self-contained on purpose. A module that referenced its origin board's libraries
would rot the moment that board moved, and these are meant to outlive the design
they came from.

**Interface ports are the design centre.** Extraction cuts the netlist at nets
that touch parts outside the selection; those cuts are the module's ports. That
is what turns a bag of components into something you can wire up: a port list is
a contract a carrier board can satisfy, and it maps directly onto KiCad's own
hierarchical sheet pins.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..emitter import loaders, sexpr
from .reader import ImportedBoard

MODULES_DIRNAME = "modules"

# Nets that cross every boundary and mean nothing as a port. Extracting a power
# block would otherwise declare GND as its defining interface.
_UBIQUITOUS = {"GND", "AGND", "DGND", "PGND", "VSS", "EARTH"}

# What KiCad names the net of a pad nothing connects to. Such a pad is open,
# not a one-member net, and goes into the module as NC: carried over as a net
# it became an internal net named after one pad of the source board.
_UNCONNECTED_PREFIX = "unconnected-"
NC = "NC"


@dataclass
class Port:
    """One net crossing the module boundary."""

    name: str
    kind: str = "signal"          # power | signal | rf
    internal_pins: list[str] = field(default_factory=list)   # refdes.pad inside
    external_refs: list[str] = field(default_factory=list)   # refdes outside it touched

    def to_dict(self) -> dict:
        return {
            "port": self.name,
            "kind": self.kind,
            "internal_pins": self.internal_pins,
            # What it was wired to on the source board — not a requirement, but
            # the fastest way to understand what a port is *for*.
            "was_connected_to": self.external_refs,
        }


@dataclass
class ModuleSpec:
    name: str
    description: str = ""
    source_board: str = ""
    source_sha: str = ""
    components: list[str] = field(default_factory=list)
    ports: list[Port] = field(default_factory=list)
    internal_nets: list[str] = field(default_factory=list)
    bom: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # refdes -> pad -> net, for every part inside. The wiring a carrier needs:
    # a module that brought only its parts arrived as loose components.
    pins: dict[str, dict[str, str]] = field(default_factory=dict)
    # Whether the source's tree had uncommitted changes when this was cut.
    # None when git could not say (no repository, no git).
    source_dirty: bool | None = None
    source_remote: str = ""
    # Not in the manifest: a path on this machine.
    source_dir: Path | None = None

    def to_manifest(self) -> dict:
        provenance = {
            "source_board": self.source_board,
            "source_sha": self.source_sha,
            "extracted_by": "blpl",
        }
        if self.source_dirty is not None:
            provenance["source_dirty"] = self.source_dirty
        if self.source_remote:
            provenance["source_remote"] = self.source_remote
        return {
            "module": self.name,
            "description": self.description,
            "provenance": provenance,
            # The contract a carrier board must satisfy.
            "interface": [p.to_dict() for p in self.ports],
            "internal_nets": self.internal_nets,
            "components": self.components,
            "bom": self.bom,
            "warnings": self.warnings,
        }


def plan_module(
    board: ImportedBoard,
    refdes: list[str],
    *,
    name: str,
    description: str = "",
    source_sha: str = "",
) -> ModuleSpec:
    """Work out what extracting these parts would produce, without writing anything.

    Separating the plan from the write is what makes extraction reviewable: the
    port list is the thing a human must agree with, and it is far easier to
    judge before a directory exists than after.
    """
    selected = [r for r in refdes if r in board.components]
    spec = ModuleSpec(
        name=name,
        description=description,
        source_board=board.name,
        source_sha=source_sha,
        components=sorted(selected),
        source_dir=board.source_dir,
    )
    if not source_sha and board.source_dir is not None:
        _record_git(spec, board.source_dir)
    missing = sorted(set(refdes) - set(selected))
    if missing:
        spec.warnings.append(f"not on this board, ignored: {', '.join(missing)}")
    if not selected:
        spec.warnings.append("nothing to extract — none of the named parts are on this board")
        return spec

    inside = set(selected)
    for net in board.nets.values():
        members_inside = [(r, p) for r, p in net.members if r in inside]
        if not members_inside:
            continue
        if net.name.startswith(_UNCONNECTED_PREFIX):
            for r, p in members_inside:
                spec.pins.setdefault(r, {})[p] = NC
            continue
        for r, p in members_inside:
            spec.pins.setdefault(r, {})[p] = net.name
        members_outside = sorted({r for r, _ in net.members if r not in inside})

        if members_outside or net.name.upper() in _UBIQUITOUS:
            # It leaves the block (or is a rail everything shares): a port.
            spec.ports.append(
                Port(
                    name=net.name,
                    kind=_port_kind(net.name),
                    internal_pins=[f"{r}.{p}" for r, p in members_inside],
                    external_refs=members_outside,
                )
            )
        else:
            spec.internal_nets.append(net.name)

    spec.ports.sort(key=lambda p: (p.kind != "power", p.name))
    spec.internal_nets.sort()

    for ref in spec.components:
        component = board.components[ref]
        row = {"refdes": ref, "value": component.value}
        if component.mpn:
            row["mpn"] = component.mpn
        if component.footprint:
            row["footprint"] = component.footprint
        if component.lib_id:
            row["symbol"] = component.lib_id
        if spec.pins.get(ref):
            row["pins"] = dict(sorted(spec.pins[ref].items(), key=lambda kv: _pad_key(kv[0])))
        spec.bom.append(row)

    no_mpn = [r["refdes"] for r in spec.bom if not r.get("mpn")]
    if no_mpn:
        # Not fatal — plenty of boards carry no MPN properties — but a module
        # you cannot source is worth flagging at extraction rather than at quote.
        spec.warnings.append(
            f"{len(no_mpn)} part(s) have no MPN on the source board: {', '.join(no_mpn[:8])}"
            + (" …" if len(no_mpn) > 8 else "")
        )
    if not spec.ports:
        spec.warnings.append(
            "no nets cross the boundary — this selection is electrically isolated, "
            "which usually means the wrong parts were chosen"
        )
    return spec


def _pad_key(pad: str) -> tuple:
    """Pads in the order a person reads them: 2 before 10, A1 before A10."""
    head = pad.rstrip("0123456789")
    tail = pad[len(head):]
    return (head, int(tail) if tail else -1, pad)


def _record_git(spec: ModuleSpec, directory: Path) -> None:
    """The commit the source board was read at, and whether its tree was dirty.

    Scoped to the board's directory, as library provenance is: an unrelated
    edit elsewhere in the repository does not make this board any less the
    committed one.
    """
    from ..core.provenance import describe_root

    git = describe_root(directory).get("git")
    if not git:
        spec.warnings.append("source board is not in a git repository: no commit recorded")
        return
    spec.source_sha = git["commit"]
    spec.source_dirty = git["dirty"]
    spec.source_remote = git.get("remote", "")
    if git["dirty"]:
        spec.warnings.append(
            f"source board had {git['changed_files']} uncommitted change(s) when extracted: "
            "the recorded commit is not exactly what this module came from"
        )


def _lib_table(directory: Path | None, table: str) -> dict[str, Path]:
    """Nickname -> library path from a board's own sym-/fp-lib-table.

    The board's table is the authority on where its parts came from: the stock
    footprint root alone missed every project and vendor footprint, which on a
    real board are the parts most worth carrying into a module.
    """
    if directory is None:
        return {}
    path = Path(directory) / table
    if not path.is_file():
        return {}
    try:
        tree = sexpr.parse(path.read_text(encoding="utf-8"))
    except (OSError, sexpr.ParseError):
        return {}
    out: dict[str, Path] = {}
    for lib in sexpr.find_all(tree, "lib"):
        name, uri = sexpr.find(lib, "name"), sexpr.find(lib, "uri")
        if not name or not uri:
            continue
        raw = sexpr.unquote(uri[1]).replace("${KIPRJMOD}", str(directory))
        if "${" in raw:
            continue  # an environment variable this process cannot resolve
        out[sexpr.unquote(name[1])] = Path(raw)
    return out


def _port_kind(net_name: str) -> str:
    upper = net_name.upper()
    if any(upper.startswith(p) for p in ("VCC", "VDD", "VBAT", "VBUS", "VIN", "VSYS", "3V3", "5V", "12V")):
        return "power"
    if upper in _UBIQUITOUS:
        return "power"
    if "RF" in upper or "ANT" in upper:
        return "rf"
    return "signal"


def write_module(
    spec: ModuleSpec,
    dest_root: Path,
    *,
    symbol_roots: list[Path] | None = None,
    footprint_root: Path | None = None,
    footprint_roots: list[Path] | None = None,
    overwrite: bool = False,
) -> tuple[Path, list[str]]:
    """Write the module directory. Returns (path, notes).

    Copying symbols and footprints in — rather than referencing them — is what
    makes a module survive the disappearance of the board it came from. A note
    is recorded for anything that could not be copied, because a module missing
    a symbol still has value and should not be refused outright, but must never
    look complete when it is not.

    Libraries are looked for first in the source board's own lib tables, then
    in ``symbol_roots`` / ``footprint_roots`` (and the single ``footprint_root``
    older callers pass), in that order.
    """
    dest = Path(dest_root) / MODULES_DIRNAME / spec.name
    if dest.exists() and not overwrite:
        raise FileExistsError(f"module {spec.name!r} already exists at {dest}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    notes: list[str] = []

    # -- symbols -------------------------------------------------------------
    #
    # Grouped by their source library, and written under that library's name, so
    # the module directory is itself a valid library root.
    wanted_symbols = sorted({row["symbol"] for row in spec.bom if row.get("symbol")})
    # A sym-lib-table entry names a file; its directory is a root as long as the
    # file carries the nickname, which is how load_symbol_def looks it up.
    table_sym_roots = [
        p.parent for nick, p in _lib_table(spec.source_dir, "sym-lib-table").items()
        if p.name in (f"{nick}.kicad_sym", f"{nick}.kicad_symdir")
    ]
    symbol_roots = list(dict.fromkeys([*table_sym_roots, *(symbol_roots or [])]))
    by_lib: dict[str, list[str]] = {}
    for ref in wanted_symbols:
        if not symbol_roots:
            notes.append(f"no libraries to copy symbols from, so {ref} was not copied")
            continue
        try:
            lib, name = loaders.split_ref(ref)
        except ValueError:
            notes.append(f"unparseable symbol reference: {ref}")
            continue
        try:
            node = loaders.load_symbol_def(ref, symbol_roots)
        except (loaders.LibraryMiss, OSError, ValueError):
            notes.append(f"symbol not found in the configured libraries: {ref}")
            continue
        # load_symbol_def qualifies the name for a schematic's lib_symbols cache;
        # inside a library file the bare name is what KiCad looks for.
        body = ["symbol", f'"{name}"', *node[2:]]
        by_lib.setdefault(lib, []).append(sexpr.dump(body))

    for lib, bodies in by_lib.items():
        (dest / f"{lib}.kicad_sym").write_text(
            "(kicad_symbol_lib (version 20251024) (generator blpl)\n"
            + "\n".join(bodies)
            + "\n)\n",
            encoding="utf-8",
        )
    if wanted_symbols and not by_lib:
        notes.append("no symbols could be copied — the module carries its BOM only")

    # -- footprints ----------------------------------------------------------
    wanted_fps = sorted({row["footprint"] for row in spec.bom if row.get("footprint")})
    fp_table = _lib_table(spec.source_dir, "fp-lib-table")
    fp_roots = [Path(r) for r in [*(footprint_roots or []), *([footprint_root] if footprint_root else [])]]
    for ref in wanted_fps:
        try:
            lib, fp_name = loaders.split_ref(ref)
        except ValueError:
            notes.append(f"unparseable footprint reference: {ref}")
            continue
        candidates = ([fp_table[lib] / f"{fp_name}.kicad_mod"] if lib in fp_table else []) + [
            r / f"{lib}.pretty" / f"{fp_name}.kicad_mod" for r in fp_roots
        ]
        if not candidates:
            notes.append(f"no footprint library configured, so {ref} was not copied")
            continue
        source = next((c for c in candidates if c.is_file()), None)
        if source is None:
            notes.append(f"footprint not found: {ref}")
            continue
        pretty = dest / f"{lib}.pretty"
        pretty.mkdir(exist_ok=True)
        shutil.copy2(source, pretty / source.name)

    (dest / "bom.json").write_text(json.dumps(spec.bom, indent=2) + "\n", encoding="utf-8")

    manifest = spec.to_manifest()
    if notes:
        manifest.setdefault("warnings", []).extend(notes)
    (dest / "module.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return dest, notes


def list_modules(*roots: Path) -> list[dict]:
    """Every module under the given roots, project-local first.

    Order matters to the caller: a project's own copy of a module should win
    over a shared one, the same way a hand-authored symbol beats a generated one.
    """
    seen: dict[str, dict] = {}
    for root in roots:
        base = Path(root) / MODULES_DIRNAME
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            manifest = d / "module.yaml"
            if not manifest.is_file() or d.name in seen:
                continue
            try:
                data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            seen[d.name] = {
                "name": d.name,
                "path": str(d),
                "root": str(root),
                "description": data.get("description", ""),
                "ports": [p.get("port") for p in data.get("interface", [])],
                "components": len(data.get("components", [])),
                "source_board": (data.get("provenance") or {}).get("source_board", ""),
            }
    return list(seen.values())
