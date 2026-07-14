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

import math
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


def _paper(width: float, height: float) -> sexpr.Node:
    """Pick the smallest standard sheet the layout fits on, else a custom one.

    The emitter used to hardcode A3 (420x297mm) regardless of what it drew. dev.04
    needs more than four times that, so most of the board was off the page.
    """
    for name, w, h in (("A4", 297.0, 210.0), ("A3", 420.0, 297.0),
                       ("A2", 594.0, 420.0), ("A1", 841.0, 594.0),
                       ("A0", 1189.0, 841.0)):
        if width <= w and height <= h:
            return ["paper", sexpr.quote(name)]
    return ["paper", '"User"', _mm(width), _mm(height)]


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
    mpn: str = "",
    manufacturer: str = "",
    datasheet: str = "",
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
        _default_property("Datasheet", datasheet, x, y, hidden=True),
        _default_property("Description", description, x, y, hidden=True),
        # A board with no MPNs cannot be quoted, ordered, or fabbed. bom.json has
        # them; the schematic is where they have to land for any BOM export — or
        # any reviewer — to see them. "MPN" and "Manufacturer" are the field names
        # KiCad's BOM tooling and kicad-happy both look for.
        _default_property("MPN", mpn, x, y, hidden=True),
        _default_property("Manufacturer", manufacturer, x, y, hidden=True),
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


PWR_FLAG_LIB_ID = "power:PWR_FLAG"


def _pwr_flag_instance(
    index: int,
    x: float,
    y: float,
    project_name: str,
    schematic_uuid: str,
    net_name: str,
) -> sexpr.Node:
    """A PWR_FLAG: the marker that tells ERC where a rail's power comes from.

    Every rail on a BLPL board is fed from outside — a connector, a battery, a
    regulator we have no symbol for — so every rail is a net of nothing but
    power_in pins, which is exactly the shape ERC calls "power pin not driven".
    The rails are fine; the schematic just never said so. PWR_FLAG says so.

    Reference is ``#FLG0N`` and the symbol is off the board and out of the BOM:
    it is an annotation, not a part, and it must never reach the PCB.
    """
    refdes = f"#FLG{index:02d}"
    return [
        "symbol",
        ["lib_id", sexpr.quote(PWR_FLAG_LIB_ID)],
        ["at", _mm(x), _mm(y), "0"],
        ["unit", "1"],
        ["body_style", "1"],
        ["exclude_from_sim", "yes"],
        ["in_bom", "no"],
        ["on_board", "no"],
        ["in_pos_files", "no"],
        ["dnp", "no"],
        ["uuid", _new_uuid()],
        _default_property("Reference", refdes, x, y - 5),
        _default_property("Value", "PWR_FLAG", x, y + 5),
        _default_property("Footprint", "", x, y, hidden=True),
        _default_property("Datasheet", "", x, y, hidden=True),
        _default_property(
            "Description", f"ERC power source marker for {net_name}", x, y, hidden=True
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


def _undriven_power_nets(
    hdm: dict, pin_net_map: dict[str, dict[str, str]], pins_by_lib_id: dict[str, list[dict]]
) -> list[str]:
    """Power rails with nothing on them that drives them.

    A rail is a rail either because Stage 4 classified it as one (Power_Bulk) or
    because something declares a power_in pin on it — matching how ERC and
    kicad-happy both decide. It is *sourced* only if some pin on it is power_out.

    Every rail on a BLPL board is fed from outside the schematic — a connector, a
    battery, a regulator whose symbol is a placeholder with passive pins — so a
    rail typically has no power_out anywhere and reads as undriven even though the
    hardware is fine. Both checks look for exactly one thing to say otherwise: a
    PWR_FLAG.
    """
    etypes_by_net: dict[str, set[str]] = {}
    components = hdm.get("components", {})
    for refdes, comp in components.items():
        lib_id = comp.get("lib_symbol") or ""
        pin_nets = pin_net_map.get(refdes) or {}
        if not pin_nets or ":" not in lib_id:
            continue
        for pin in pins_by_lib_id.get(lib_id, []):
            net_name = pin_nets.get(pin["number"])
            if net_name:
                etypes_by_net.setdefault(net_name, set()).add(pin.get("etype", ""))

    rails: list[str] = []
    for net_name, net_def in (hdm.get("nets") or {}).items():
        etypes = etypes_by_net.get(net_name, set())
        is_rail = net_def.get("class") == "Power_Bulk" or "power_in" in etypes
        if is_rail and "power_out" not in etypes:
            rails.append(net_name)
    return sorted(rails)


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

    A pin's ``(at x y rot)`` is its *connection point*, and ``rot`` is the
    direction the pin body is drawn in FROM that point — i.e. inward, toward the
    symbol. Conn_01x18 pin 1 is ``(at -5.08 20.32 0)`` with length 3.81, and the
    symbol body starts at x=-1.27: the pin runs rightward, into the body.

    So a stub must run the opposite way, at rot+180. We had it running along rot,
    which drew every stub back across the symbol it came from and left the wire
    dangling instead of connecting.
    """
    import math
    angle = math.radians(rot + 180.0)
    dx = math.cos(angle) * stub_len
    dy = -math.sin(angle) * stub_len  # schematic Y inverted relative to library
    return (_snap(tip_x + dx), _snap(tip_y + dy))


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


# KiCad's schematic connection grid. A pin or wire endpoint that is not a
# multiple of this does not connect — it just looks like it does. Every position
# the emitter chooses has to be a multiple of it, so every layout constant here
# is expressed in grid units rather than millimetres.
GRID_MM = 1.27


def _snap(v: float) -> float:
    """Snap a coordinate onto the connection grid."""
    return round(v / GRID_MM) * GRID_MM


# KiCad's stroke font at size 1.27 is about 1.1mm per character advance. Round up
# to 1.4: a label that is wider than we budgeted for reaches into the neighbouring
# component's pins, and coincident points are, to KiCad, a wire.
_CHAR_MM = 1.4
_STUB_MM = 2 * GRID_MM  # 2.54mm


def _cell_size(hdm: dict, symbols_root: Path) -> tuple[float, float]:
    """How much room one component needs: its body, its stubs, and its labels.

    A label is text, and text takes space on the sheet. If a cell is narrower
    than symbol + stub + label, one component's labels reach across into its
    neighbour's pins — and KiCad reads coincident points as connected.
    """
    components = hdm.get("components", {}) or {}
    sym_w = sym_h = 0.0
    for comp in components.values():
        lib_id = comp.get("lib_symbol") or ""
        if ":" not in lib_id:
            continue
        try:
            w, h = loaders.symbol_extents(lib_id, symbols_root)
        except loaders.LibraryMiss:
            continue
        sym_w, sym_h = max(sym_w, w), max(sym_h, h)

    longest = max((len(n) for n in (hdm.get("nets") or {})), default=8)
    label_mm = longest * _CHAR_MM

    # Labels sit on both sides of a symbol, so the stub+label allowance is doubled.
    cell_w = sym_w + 2 * (_STUB_MM + label_mm) + 4 * GRID_MM
    cell_h = sym_h + 8 * GRID_MM  # room for Reference/Value text above and below
    return (_snap(cell_w), _snap(cell_h))


def _component_grid(
    components: dict,
    cell_w: float,
    cell_h: float,
    cols: int,
    origin_x: float = 20 * GRID_MM,
    origin_y: float = 20 * GRID_MM,
) -> list[tuple[str, float, float]]:
    """Arrange components on a grid sized to the widest and tallest symbol.

    Two things were wrong with the fixed 50mm/5-column grid this replaces. 50 is
    not a multiple of 1.27, so every component — and every pin and wire endpoint
    hanging off it — sat off KiCad's connection grid and could not connect. And
    50mm is smaller than a 30-pin connector, so symbols overlapped bodily and
    KiCad read the overlapping pins as wired together, shorting unrelated nets.
    """
    layout = []
    for i, refdes in enumerate(components.keys()):
        col, row = i % cols, i // cols
        layout.append(
            (refdes, _snap(origin_x + col * cell_w), _snap(origin_y + row * cell_h))
        )
    return layout


def build(hdm: dict, *, symbols_root: Path) -> sexpr.Node:
    """Build a v10 (kicad_sch ...) node from an HDM dict."""
    project = hdm.get("project", {})
    project_name = str(project.get("name", "HDM_Project"))
    components = hdm.get("components", {})
    nets = hdm.get("nets", {})

    schematic_uuid = _new_uuid()

    # Lay the sheet out first: the paper size depends on how much sheet the
    # components actually need, and a symbol that runs off the page is a symbol
    # whose pins are unreachable.
    cell_w, cell_h = _cell_size(hdm, symbols_root)
    n = max(1, len(components))
    cols = max(1, min(6, math.ceil(math.sqrt(n))))
    rows = math.ceil(n / cols)
    sheet_w = _snap(2 * 20 * GRID_MM + cols * cell_w)
    sheet_h = _snap(2 * 20 * GRID_MM + rows * cell_h + 24 * GRID_MM)  # + PWR_FLAG row

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
        _paper(sheet_w, sheet_h),
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
                "Details: .pipeline/manual_library_work.md",
                x=20.0,
                y=20.0,
            )
        )

    # No global-label "net palette". It used to be emitted as a convenience — a
    # grid of every net name, to drag onto pins in the editor — and it was a net
    # of its own problems: 193 dangling labels, 117 collisions with the per-pin
    # labels of the same name, and, because the palette entries were spaced more
    # tightly than their own text, labels that overlapped each other and shorted
    # unrelated nets together (KiCad: "Both NC_JMIC_8 and VCC_3V3 are attached to
    # the same items"). Every pin that has a net already carries its own label.

    # Component instances + per-pin label stubs for declared nets.
    pin_net_map = _build_component_pin_net_map(hdm)

    pins_by_lib_id: dict[str, list[dict]] = {}
    for comp in components.values():
        lib_id = comp.get("lib_symbol") or ""
        if ":" not in lib_id or lib_id in pins_by_lib_id:
            continue
        try:
            pins_by_lib_id[lib_id] = loaders.load_symbol_pins(lib_id, symbols_root)
        except loaders.LibraryMiss:
            pins_by_lib_id[lib_id] = []

    stub_len = 2.54  # one grid unit; enough to clear the symbol body.
    for refdes, x, y in _component_grid(components, cell_w, cell_h, cols):
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
                mpn=str(comp.get("mpn", "")),
                manufacturer=str(comp.get("manufacturer", "")),
                datasheet=str(comp.get("datasheet", "")),
            )
        )

        component_nets = pin_net_map.get(refdes) or {}
        if not component_nets:
            continue
        for pin in pins_by_lib_id.get(lib_id, []):
            net_name = component_nets.get(pin["number"])
            if not net_name:
                continue
            tip_x, tip_y = _pin_absolute_tip(x, y, pin)
            end_x, end_y = _pin_stub_endpoint(tip_x, tip_y, pin["rot"], stub_len)
            node.append(_wire(tip_x, tip_y, end_x, end_y))
            node.append(_label(net_name, end_x, end_y, rot=pin["rot"]))

    # PWR_FLAGs. Placed in a row along the bottom, each with a label naming the
    # rail it declares a source for. PWR_FLAG's pin sits at the symbol origin with
    # zero length, so a label at the same point is the connection — no stub needed.
    undriven = _undriven_power_nets(hdm, pin_net_map, pins_by_lib_id)
    if undriven:
        _add(PWR_FLAG_LIB_ID)
        for i, net_name in enumerate(undriven):
            fx = _snap(20 * GRID_MM + (i % 12) * 20 * GRID_MM)
            fy = _snap(20 * GRID_MM + rows * cell_h + 8 * GRID_MM + (i // 12) * 8 * GRID_MM)
            node.append(
                _pwr_flag_instance(i + 1, fx, fy, project_name, schematic_uuid, net_name)
            )
            node.append(_label(net_name, fx, fy))

    node.append(["sheet_instances", ["path", '"/"', ["page", '"1"']]])
    node.append(["embedded_fonts", "no"])
    return node


def flagged_nets(hdm: dict, *, symbols_root: Path) -> list[str]:
    """The nets this emitter puts a PWR_FLAG on. Recorded so Stage 8 can tell an
    unsourced rail apart from one whose source it simply cannot see."""
    pin_net_map = _build_component_pin_net_map(hdm)
    pins_by_lib_id: dict[str, list[dict]] = {}
    for comp in (hdm.get("components") or {}).values():
        lib_id = comp.get("lib_symbol") or ""
        if ":" not in lib_id or lib_id in pins_by_lib_id:
            continue
        try:
            pins_by_lib_id[lib_id] = loaders.load_symbol_pins(lib_id, symbols_root)
        except loaders.LibraryMiss:
            pins_by_lib_id[lib_id] = []
    return _undriven_power_nets(hdm, pin_net_map, pins_by_lib_id)


def emit(hdm: dict, *, symbols_root: Path) -> str:
    return sexpr.dump_top(build(hdm, symbols_root=symbols_root))


def write(hdm: dict, output_path: Path, *, symbols_root: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(emit(hdm, symbols_root=symbols_root), encoding="utf-8")
    return output_path
