"""v10 ``.kicad_sch`` emitter.

Produces a schematic that kicad-cli 9.x / 10.x can load for ERC. Each HDM
component is placed as a symbol instance with a ``lib_id`` reference. KiCad
resolves the symbol graphics from the project's symbol-library table at
open-time, so the ``lib_symbols`` block here only carries empty-shell
declarations (matching what KiCad itself emits).

Nets are declared as ``(global_label ...)`` entries laid out on a grid — a
"net palette" the user can drag onto pins in the editor. No wires are drawn;
ERC will report unconnected pins, which is the correct signal for a freshly
synthesized design that hasn't been routed yet.

The schematic-to-PCB linkage uses matching net names. Component Footprint
properties carry the ``Lib:Name`` footprint ref so that ``Update PCB from
Schematic`` is a no-op on our generated pair.
"""

from __future__ import annotations

import uuid as _uuid
from copy import deepcopy
from pathlib import Path

from . import loaders, sexpr


_SCH_VERSION = "20260306"
_GENERATOR_VERSION = "10.0"
_SCHEMATIC_UUID = None  # Set per-run in build().


def _new_uuid() -> str:
    return f'"{_uuid.uuid4()}"'


def _mm(v: float) -> str:
    """Format a coordinate consistently; schematics use mm at 0.001 precision."""
    return f"{round(v, 3)}"


def _empty_font() -> sexpr.Node:
    return ["effects", ["font", ["size", "1.27", "1.27"]]]


def _default_property(
    name: str, value: str, x: float, y: float, *, hidden: bool = False
) -> sexpr.Node:
    prop: sexpr.Node = [
        "property",
        sexpr.quote(name),
        sexpr.quote(value),
        ["at", _mm(x), _mm(y), "0"],
        ["show_name", "no"],
        ["do_not_autoplace", "no"],
    ]
    if hidden:
        prop.append(["hide", "yes"])
    prop.append(_empty_font())
    return prop


def _component_instance(
    refdes: str,
    lib_id: str,
    value: str,
    footprint: str,
    x: float,
    y: float,
    project_name: str,
    schematic_uuid: str,
    description: str = "",
    requested_symbol: str = "",
) -> sexpr.Node:
    return [
        "symbol",
        ["lib_id", sexpr.quote(lib_id)],
        ["at", _mm(x), _mm(y), "0"],
        ["unit", "1"],
        ["body_style", "1"],
        ["exclude_from_sim", "no"],
        ["in_bom", "yes"],
        ["on_board", "yes"],
        ["in_pos_files", "yes"],
        ["dnp", "no"],
        ["uuid", _new_uuid()],
        _default_property("Reference", refdes, x, y - 10),
        _default_property("Value", value, x, y + 10),
        _default_property("Footprint", footprint, x, y + 15, hidden=True),
        _default_property("Datasheet", "", x, y, hidden=True),
        _default_property("Description", description, x, y, hidden=True),
        # Visible, on the symbol, in KiCad and in any browser viewer. A placeholder
        # you can't see is a placeholder that gets fabricated.
        *(
            [
                _default_property(
                    "BLPL_PLACEHOLDER",
                    f"NOT A REAL SYMBOL — draw {requested_symbol} manually",
                    x,
                    y + 20,
                )
            ]
            if requested_symbol
            else []
        ),
        [
            "instances",
            [
                "project",
                sexpr.quote(project_name),
                [
                    "path",
                    sexpr.quote(f"/{sexpr.unquote(schematic_uuid)}"),
                    ["reference", sexpr.quote(refdes)],
                    ["unit", "1"],
                ],
            ],
        ],
    ]


def _extends_of(shell: sexpr.Sexp) -> str | None:
    """The parent symbol name in a derived symbol's ``(extends "Parent")``, if any."""
    if not isinstance(shell, list):
        return None
    for child in shell:
        if isinstance(child, list) and len(child) >= 2 and child[0] == "extends":
            return sexpr.unquote(str(child[1]))
    return None


def _warning_text(message: str, x: float, y: float) -> sexpr.Node:
    """A free text block on the schematic sheet, for things the user must not miss."""
    return [
        "text",
        sexpr.quote(message),
        ["exclude_from_sim", "no"],
        ["at", _mm(x), _mm(y), "0"],
        ["effects", ["font", ["size", "3", "3"], ["bold", "yes"]], ["justify", "left"]],
        ["uuid", _new_uuid()],
    ]


def _global_label(net_name: str, x: float, y: float) -> sexpr.Node:
    return [
        "global_label",
        sexpr.quote(net_name),
        ["shape", "input"],
        ["at", _mm(x), _mm(y), "0"],
        ["effects", ["font", ["size", "1.27", "1.27"]], ["justify", "left"]],
        ["uuid", _new_uuid()],
        [
            "property",
            '"Intersheetrefs"',
            '"${INTERSHEET_REFS}"',
            ["at", _mm(x), _mm(y), "0"],
            ["hide", "yes"],
            ["show_name", "no"],
            ["do_not_autoplace", "no"],
            _empty_font(),
        ],
    ]


def _title_block(project: dict) -> sexpr.Node:
    return [
        "title_block",
        ["title", sexpr.quote(str(project.get("name", "Untitled")))],
        ["rev", sexpr.quote(str(project.get("board_id", "1.0")))],
    ]


def _pin_absolute_tip(comp_x: float, comp_y: float, pin: dict) -> tuple[float, float]:
    """Schematic-coord absolute position of a pin's electrical endpoint.

    Library symbols are defined with Y-up; schematic coords are Y-down. We
    flip the pin's local Y when projecting into the schematic frame. Component
    rotation is assumed 0 for the auto-placed grid (the only layout the
    emitter currently produces).
    """
    return (comp_x + pin["x"], comp_y - pin["y"])


def _pin_stub_endpoint(tip_x: float, tip_y: float, rot: float, stub_len: float) -> tuple[float, float]:
    """Where the far end of a pin's label-stub wire lands.

    Pin rotation is the direction the pin extends OUT from the body (0 = right,
    90 = up, 180 = left, 270 = down — library convention). A net label stub
    should extend further in the same outward direction so it doesn't collide
    with the symbol body.
    """
    import math
    angle = math.radians(rot)
    dx = math.cos(angle) * stub_len
    dy = -math.sin(angle) * stub_len  # schematic Y inverted relative to library
    return (tip_x + dx, tip_y + dy)


def _wire(x1: float, y1: float, x2: float, y2: float) -> sexpr.Node:
    return [
        "wire",
        ["pts", ["xy", _mm(x1), _mm(y1)], ["xy", _mm(x2), _mm(y2)]],
        ["stroke", ["width", "0"], ["type", "default"]],
        ["uuid", _new_uuid()],
    ]


def _label(net_name: str, x: float, y: float, rot: float = 0) -> sexpr.Node:
    return [
        "label",
        sexpr.quote(net_name),
        ["at", _mm(x), _mm(y), f"{rot}"],
        ["effects", ["font", ["size", "1.27", "1.27"]], ["justify", "left", "bottom"]],
        ["uuid", _new_uuid()],
    ]


def _build_component_pin_net_map(hdm: dict) -> dict[str, dict[str, str]]:
    """Return ``{refdes: {physical_pin: net_name}}`` by resolving each net's pad list through pin_map."""
    components = hdm.get("components", {}) or {}
    nets = hdm.get("nets", {}) or {}
    result: dict[str, dict[str, str]] = {}
    pin_map_cache: dict[str, dict[str, str]] = {}
    for net_name, net_def in nets.items():
        if not net_name:
            continue
        for pair in (net_def.get("pads") or []):
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            refdes, logical = str(pair[0]), str(pair[1])
            if refdes not in pin_map_cache:
                comp = components.get(refdes, {}) or {}
                pin_map_cache[refdes] = {
                    str(k): str(v) for k, v in (comp.get("pin_map") or {}).items()
                }
            physical = pin_map_cache[refdes].get(logical, logical)
            result.setdefault(refdes, {})[physical] = net_name
    return result


def _component_grid(components: dict, origin_x: float = 50, origin_y: float = 50, step: float = 50) -> list[tuple[str, float, float]]:
    """Arrange components on a simple grid for the schematic sheet."""
    layout = []
    cols = 5
    for i, refdes in enumerate(components.keys()):
        col, row = i % cols, i // cols
        layout.append((refdes, origin_x + col * step, origin_y + row * step))
    return layout


def _label_grid(net_names: list[str], origin_x: float = 50, origin_y: float = 200, step_x: float = 30, step_y: float = 10) -> list[tuple[str, float, float]]:
    """Arrange global labels on a grid beneath the components."""
    layout = []
    cols = 8
    for i, name in enumerate(net_names):
        col, row = i % cols, i // cols
        layout.append((name, origin_x + col * step_x, origin_y + row * step_y))
    return layout


def build(hdm: dict, *, symbols_root: Path) -> sexpr.Node:
    """Build a v10 (kicad_sch ...) node from an HDM dict."""
    project = hdm.get("project", {})
    project_name = str(project.get("name", "HDM_Project"))
    components = hdm.get("components", {})
    nets = hdm.get("nets", {})

    schematic_uuid = _new_uuid()

    # Gather the unique lib_ids (from each component's lib_symbol field, if any).
    lib_ids: list[str] = []
    seen: set[str] = set()
    for comp in components.values():
        ref = comp.get("lib_symbol")
        if ref and ":" in ref and ref not in seen:
            seen.add(ref)
            lib_ids.append(ref)

    lib_symbols_children: list[sexpr.Sexp] = ["lib_symbols"]
    missing: list[str] = []
    emitted: set[str] = set()

    def _add(ref: str) -> None:
        """Emit a symbol shell, pulling in any parent it derives from.

        KiCad has derived symbols: a shell can carry (extends "Parent"), and the
        parent supplies the pins and body. Copying only the child produces a
        schematic that references a base symbol which isn't in the file, and KiCad
        rejects the whole thing with a bare "Failed to load schematic". So follow
        the extends chain and bring the parents along.
        """
        if ref in emitted:
            return
        shell = loaders.load_symbol_shell(ref, symbols_root)
        parent = _extends_of(shell)
        if parent:
            lib, _, _ = ref.partition(":")
            _add(f"{lib}:{parent}")  # parent must appear before the child
        emitted.add(ref)
        lib_symbols_children.append(deepcopy(shell))

    for ref in lib_ids:
        try:
            _add(ref)
        except loaders.LibraryMiss:
            # Swallowing this used to be silent, which produced a schematic whose
            # symbol instances carry a lib_id with no matching lib_symbols entry.
            # KiCad and every browser viewer choke on that — they try to resolve
            # the reference, find nothing, and hang or bail. A board that cannot
            # be opened is worse than one that fails to build, so say so.
            missing.append(ref)
            continue

    if missing:
        raise loaders.LibraryMiss(
            f"{len(missing)} symbol(s) could not be loaded from {symbols_root}: "
            f"{', '.join(missing[:8])}"
            + (f", … (+{len(missing) - 8} more)" if len(missing) > 8 else "")
            + ". Emitting them anyway would produce a schematic with dangling lib_ids "
            "that no viewer can open. Check that the symbol libraries are present and "
            "that Stage 1/2 resolved these names to symbols that actually exist."
        )

    node: sexpr.Node = [
        "kicad_sch",
        ["version", _SCH_VERSION],
        ["generator", '"blpl.emitter"'],
        ["generator_version", f'"{_GENERATOR_VERSION}"'],
        ["uuid", schematic_uuid],
        ["paper", '"A3"'],
        _title_block(project),
        lib_symbols_children,
    ]

    # A banner drawn on the sheet itself. Every other warning lives in a log file
    # or a report someone has to go and read; this one is unavoidable the moment
    # the board is opened — in KiCad, or in the browser viewer.
    placeholders = sorted(
        refdes for refdes, c in components.items() if c.get("needs_manual_symbol")
    )
    if placeholders:
        shown = ", ".join(placeholders[:10]) + (
            f" (+{len(placeholders) - 10} more)" if len(placeholders) > 10 else ""
        )
        node.append(
            _warning_text(
                f"*** {len(placeholders)} PLACEHOLDER SYMBOL(S) — DO NOT FABRICATE THIS BOARD ***\n"
                f"{shown}\n"
                "These parts have NO REAL SYMBOL. Generic stand-ins were emitted so the "
                "board would open.\n"
                "Draw the real symbols, save them into the project's libraries/symbols/ "
                "directory, then re-run stage5.\n"
                "Details: .pipeline/manual_symbols_required.md",
                x=20.0,
                y=20.0,
            )
        )

    # Global labels (net palette).
    net_names = [n for n in nets.keys() if n]
    for name, x, y in _label_grid(net_names):
        node.append(_global_label(name, x, y))

    # Component instances + per-pin label stubs for declared nets.
    pin_net_map = _build_component_pin_net_map(hdm)
    stub_len = 2.54  # one grid unit; enough to clear the symbol body.
    for refdes, x, y in _component_grid(components):
        comp = components[refdes]
        lib_id = comp.get("lib_symbol") or ""
        if ":" not in lib_id:
            # Without a resolvable lib_symbol, we skip the instance: KiCad would
            # flag a lib_id mismatch otherwise. The user can add it after editing
            # bom.json and re-running the pipeline.
            continue
        placeholder = bool(comp.get("needs_manual_symbol"))
        node.append(
            _component_instance(
                refdes=refdes,
                lib_id=lib_id,
                value=str(comp.get("value", refdes)),
                footprint=str(comp.get("footprint", "")),
                x=x,
                y=y,
                project_name=project_name,
                schematic_uuid=schematic_uuid,
                description=(
                    "PLACEHOLDER SYMBOL — this is not the real part."
                    if placeholder
                    else ""
                ),
                requested_symbol=str(comp.get("requested_symbol", "")) if placeholder else "",
            )
        )

        component_nets = pin_net_map.get(refdes) or {}
        if not component_nets:
            continue
        try:
            pins = loaders.load_symbol_pins(lib_id, symbols_root)
        except loaders.LibraryMiss:
            pins = []
        for pin in pins:
            net_name = component_nets.get(pin["number"])
            if not net_name:
                continue
            tip_x, tip_y = _pin_absolute_tip(x, y, pin)
            end_x, end_y = _pin_stub_endpoint(tip_x, tip_y, pin["rot"], stub_len)
            node.append(_wire(tip_x, tip_y, end_x, end_y))
            node.append(_label(net_name, end_x, end_y, rot=pin["rot"]))

    node.append(["sheet_instances", ["path", '"/"', ["page", '"1"']]])
    node.append(["embedded_fonts", "no"])
    return node


def emit(hdm: dict, *, symbols_root: Path) -> str:
    return sexpr.dump_top(build(hdm, symbols_root=symbols_root))


def write(hdm: dict, output_path: Path, *, symbols_root: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(emit(hdm, symbols_root=symbols_root), encoding="utf-8")
    return output_path
