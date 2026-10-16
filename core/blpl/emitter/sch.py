"""v10 ``.kicad_sch`` emitter.

Produces a schematic that kicad-cli 9.x / 10.x can load for ERC. Each HDM
component is placed as a symbol instance with a ``lib_id`` reference. KiCad
resolves the symbol graphics from the ``lib_symbols`` block, which carries a
full, self-contained copy of every symbol used — derived symbols flattened
against their parents, because KiCad cannot resolve ``(extends ...)`` inside a
schematic cache.

Each pin that has a net gets a short stub wire and a label at its far end;
same-named labels are what join pins into nets. No net palette and no
pin-to-pin wires.

The schematic-to-PCB linkage uses matching net names. Component Footprint
properties carry the ``Lib:Name`` footprint ref so that ``Update PCB from
Schematic`` is a no-op on our generated pair.
"""

from __future__ import annotations

import math
import re
import uuid as _uuid
from copy import deepcopy
from pathlib import Path

from . import loaders, sch_layout, sexpr


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
    name: str, value: str, x: float, y: float, *, hidden: bool = False, justify: str | None = None,
    angle: str = "0",
) -> sexpr.Node:
    prop: sexpr.Node = [
        "property",
        sexpr.quote(name),
        sexpr.quote(value),
        ["at", _mm(x), _mm(y), angle],
        ["show_name", "no"],
        ["do_not_autoplace", "no"],
    ]
    if hidden:
        prop.append(["hide", "yes"])
    effects = _empty_font()
    if justify:
        effects = [*effects, ["justify", justify]]
    prop.append(effects)
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
    unit: int = 1,
    rot: int = 0,
    ref_at: tuple[float, float, str | None] | None = None,
    value_at: tuple[float, float, str | None] | None = None,
) -> sexpr.Node:
    rx, ry, rj = ref_at or (x, y - 10, None)
    vx, vy, vj = value_at or (x, y + 10, None)
    # KiCad turns a field with its symbol; turning it back keeps a resistor
    # lying on its side labelled in horizontal text.
    field_angle = str(rot % 180)
    return [
        "symbol",
        ["lib_id", sexpr.quote(lib_id)],
        ["at", _mm(x), _mm(y), str(rot)],
        ["unit", str(unit)],
        ["body_style", "1"],
        ["exclude_from_sim", "no"],
        ["in_bom", "yes"],
        ["on_board", "yes"],
        ["in_pos_files", "yes"],
        ["dnp", "no"],
        ["uuid", _new_uuid()],
        _default_property("Reference", refdes, rx, ry, justify=rj, angle=field_angle),
        _default_property("Value", value, vx, vy, justify=vj, angle=field_angle),
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
                    ["unit", str(unit)],
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


def _property_name(node: sexpr.Sexp) -> str | None:
    """The name of a ``(property "Name" "Value" ...)`` node."""
    if isinstance(node, list) and len(node) >= 2 and node[0] == "property":
        return sexpr.unquote(str(node[1]))
    return None


def _sub_symbol_name(node: sexpr.Sexp) -> str | None:
    """The name of a nested ``(symbol "Parent_0_1" ...)`` body/pin unit."""
    if isinstance(node, list) and len(node) >= 2 and node[0] == "symbol":
        return sexpr.unquote(str(node[1]))
    return None


def _flatten_derived(child: sexpr.Sexp, parent: sexpr.Sexp) -> sexpr.Node:
    """Resolve ``(extends "Parent")`` into a self-contained symbol.

    KiCad's schematic ``lib_symbols`` is a *cache*: entries are keyed by the
    full ``"Lib:Name"``, but a derived symbol names its parent bare —
    ``(extends "BQ27441-G1")``, not ``"Battery_Management:BQ27441-G1"``. So the
    parent is in the file and the child still cannot find it, and the child
    loads with **no pins at all**.

    That is not a cosmetic problem. A pinless symbol means every wire this
    emitter drew to one of its pins ends in mid-air, and every net that had one
    pin on such a symbol collapses to a single-pin net. On example-handheld's
    core board, two derived symbols — BQ27441DRZR-G1A and PAM8302AAS — produced
    37 of 53 ERC violations this way: 2 "doesn't match copy in library", 17
    "unconnected wire endpoint", 18 "label connected to only one pin". The
    stubs were at the right coordinates the whole time; there was nothing there
    to meet them.

    So the cache entry is flattened instead: the parent's graphics and pins,
    under the child's name, carrying the child's own properties. Nothing is
    left to resolve at load time, which is also what makes KiCad's
    schematic-versus-library comparison agree.
    """
    child_name = sexpr.unquote(str(child[1]))
    # Top-level cache entries are keyed "Lib:Name", but the nested body/pin
    # units are named from the *bare* symbol name — "BQ27441-G1_1_1", never
    # "Battery_Management:BQ27441-G1_1_1". Renaming against the qualified name
    # produced units called plain "Lib:Name", and KiCad answered the whole file
    # with "Failed to load schematic".
    child_bare = child_name.rpartition(":")[2] or child_name

    child_props = [c for c in child[2:] if _property_name(c) is not None]
    child_prop_names = {_property_name(c) for c in child_props}

    config: list[sexpr.Sexp] = []
    parent_props: list[sexpr.Sexp] = []
    units: list[sexpr.Sexp] = []
    trailing: list[sexpr.Sexp] = []

    for node in parent[2:]:
        tag = sexpr.head(node)
        if tag == "extends":
            continue  # resolved by the recursion that got us here
        if _property_name(node) is not None:
            # The child's own value wins; the parent supplies what it omits.
            if _property_name(node) not in child_prop_names:
                parent_props.append(deepcopy(node))
            continue
        if _sub_symbol_name(node) is not None:
            unit = deepcopy(node)
            # "Parent_1_1" -> "Child_1_1". The suffix is read off the unit's own
            # name rather than sliced against the parent's, which only works
            # when both are spelled the same way and they are not.
            sub = _sub_symbol_name(unit) or ""
            suffix = re.search(r"(_\d+_\d+)$", sub)
            unit[1] = sexpr.quote(child_bare + (suffix.group(1) if suffix else ""))
            units.append(unit)
            continue
        if tag == "embedded_fonts":
            trailing.append(deepcopy(node))
            continue
        config.append(deepcopy(node))

    flat: sexpr.Node = ["symbol", sexpr.quote(child_name)]
    flat.extend(config)
    flat.extend(deepcopy(c) for c in child_props)
    flat.extend(parent_props)
    flat.extend(units)
    flat.extend(trailing)
    return flat


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


def _wire(x1: float, y1: float, x2: float, y2: float) -> sexpr.Node:
    return [
        "wire",
        ["pts", ["xy", _mm(x1), _mm(y1)], ["xy", _mm(x2), _mm(y2)]],
        ["stroke", ["width", "0"], ["type", "default"]],
        ["uuid", _new_uuid()],
    ]


def _no_connect(x: float, y: float) -> sexpr.Node:
    """The X that tells ERC a pin is unconnected on purpose.

    A pin the design declares NC (``NC``, ``N/C``, ``NC_CELL_31``, a bare dash)
    is dropped by Stage 4 and so gets no wire and no label here — which is
    exactly what an unconnected pin looks like to ERC and to kicad-happy's
    NT-001, and the active design carried some fifty of those as findings
    that no edit to the markdown could ever clear. The marker is how KiCad
    distinguishes "left open by decision" from "forgot to wire it".
    """
    return ["no_connect", ["at", _mm(x), _mm(y)], ["uuid", _new_uuid()]]


POWER_RAIL_LIB_ID = "power:VCC"
POWER_GROUND_LIB_ID = "power:GND"


def _power_instance(
    lib_id: str, refdes: str, net: str, x: float, y: float, rot: int,
    project_name: str, schematic_uuid: str,
) -> sexpr.Node:
    """A power symbol for one rail. Its Value is the net's name, and is what
    KiCad names the net after — so one symbol serves every rail."""
    ox, oy = sch_layout._rot(0, -2.54 if lib_id == POWER_RAIL_LIB_ID else 2.54, rot)
    tx, ty = sch_layout._rot(0, -5.08 if lib_id == POWER_RAIL_LIB_ID else 5.08, rot)
    return [
        "symbol",
        ["lib_id", sexpr.quote(lib_id)],
        ["at", _mm(x), _mm(y), str(rot)],
        ["unit", "1"],
        ["body_style", "1"],
        ["exclude_from_sim", "no"],
        ["in_bom", "no"],
        ["on_board", "no"],
        ["in_pos_files", "no"],
        ["dnp", "no"],
        ["uuid", _new_uuid()],
        _default_property("Reference", refdes, x + ox, y + oy, hidden=True),
        _default_property("Value", net, x + tx, y + ty),
        _default_property("Footprint", "", x, y, hidden=True),
        _default_property("Datasheet", "", x, y, hidden=True),
        _default_property("Description", f"Power symbol: {net}", x, y, hidden=True),
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


def _global_label(net: str, x: float, y: float, angle: int) -> sexpr.Node:
    """A label whose net is ``NET`` everywhere — the name the board uses.

    A local label names its net ``/NET``, and the board's pads carry ``NET``;
    that mismatch alone was 87 schematic-parity errors on verdacor-ear-tag.
    ``angle`` is the direction the label runs away from the pin.
    """
    justify = "right" if angle in (180, 270) else "left"
    return [
        "global_label",
        sexpr.quote(net),
        ["shape", "passive"],
        ["at", _mm(x), _mm(y), str(angle)],
        ["fields_autoplaced", "yes"],
        ["effects", ["font", ["size", "1.27", "1.27"]], ["justify", justify]],
        ["uuid", _new_uuid()],
        [
            "property",
            '"Intersheetrefs"',
            '"${INTERSHEET_REFS}"',
            ["at", _mm(x), _mm(y), "0"],
            ["effects", ["font", ["size", "1.27", "1.27"]], ["hide", "yes"]],
        ],
    ]


def _junction(x: float, y: float) -> sexpr.Node:
    """The dot that marks three wires meeting, where a run's tie passes a stub."""
    return ["junction", ["at", _mm(x), _mm(y)], ["diameter", "0"], ["color", "0", "0", "0", "0"],
            ["uuid", _new_uuid()]]


def _same_net_runs(
    pins: list[dict], tips: list[tuple[float, float, int]], nets: dict[str, str]
) -> list[list[tuple[str, float, float, int]]]:
    """Pins with a net, grouped into runs of neighbours on one edge on one net.

    Neighbours in the order they sit along the edge, counting every pin on it,
    so a run never jumps a pin on another net. A one-pin run is just a pin.
    """
    by_edge: dict[int, list[tuple[float, str | None, float, float]]] = {}
    for pin, (tx, ty, d) in zip(pins, tips):
        along = ty if d in (0, 180) else tx
        by_edge.setdefault(d, []).append((along, nets.get(pin["number"]), tx, ty))
    runs: list[list[tuple[str, float, float, int]]] = []
    for d, edge in by_edge.items():
        edge.sort()
        current: list[tuple[str, float, float, int]] = []
        for along, net, tx, ty in edge:
            if current and net == current[-1][0] and net is not None and (
                (d in (0, 180) and tx == current[-1][1]) or (d in (90, 270) and ty == current[-1][2])
            ):
                current.append((net, tx, ty, d))
                continue
            if current:
                runs.append(current)
            current = [(net, tx, ty, d)] if net else []
        if current:
            runs.append(current)
    return runs


def _frame(block: "sch_layout.Block") -> list[sexpr.Node]:
    """The dashed box and title that make a block read as one circuit."""
    return [
        [
            "rectangle",
            ["start", _mm(block.x0), _mm(block.y0)],
            ["end", _mm(block.x1), _mm(block.y1)],
            ["stroke", ["width", "0"], ["type", "dash"]],
            ["fill", ["type", "none"]],
            ["uuid", _new_uuid()],
        ],
        [
            "text",
            sexpr.quote(block.title),
            ["exclude_from_sim", "no"],
            ["at", _mm(block.x0 + 2.54), _mm(block.y0 + 3.81), "0"],
            ["effects", ["font", ["size", "2.54", "2.54"], ["bold", "yes"]], ["justify", "left", "bottom"]],
            ["uuid", _new_uuid()],
        ],
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


def _load_units(components: dict, symbols_root: Path) -> dict[str, dict[int, list[dict]]]:
    """Pins per unit for every lib_symbol in use; a missing symbol reads as unit 1, no pins."""
    out: dict[str, dict[int, list[dict]]] = {}
    for comp in components.values():
        lib_id = comp.get("lib_symbol") or ""
        if ":" not in lib_id or lib_id in out:
            continue
        try:
            out[lib_id] = loaders.load_symbol_units(lib_id, symbols_root)
        except loaders.LibraryMiss:
            out[lib_id] = {1: []}
    return out


def _all_unit_pins(units_by_lib_id: dict[str, dict[int, list[dict]]]) -> dict[str, list[dict]]:
    """The flat per-symbol pin list the rail audit wants: every unit's pins.

    The audit decides whether a rail is driven from the electrical types of the
    pins on it. Feed it unit 1 alone and every power pin on a separate power
    unit is invisible — which is precisely where MCU vendors put them.
    """
    return {lib_id: [p for unit in sorted(units) for p in units[unit]] for lib_id, units in units_by_lib_id.items()}


def build(hdm: dict, *, symbols_root: Path) -> sexpr.Node:
    """Build a v10 (kicad_sch ...) node from an HDM dict."""
    project = hdm.get("project", {})
    project_name = str(project.get("name", "HDM_Project"))
    components = hdm.get("components", {})
    nets = hdm.get("nets", {})

    schematic_uuid = _new_uuid()

    # Lay the sheet out first: the paper size depends on how much sheet the
    # components actually need, and a symbol that runs off the page is a symbol
    # whose pins are unreachable. See sch_layout for how parts are grouped
    # into blocks and turned the conventional way round.
    units_by_lib_id = _load_units(components, symbols_root)
    pin_net_map = _build_component_pin_net_map(hdm)
    pins_by_lib_id = _all_unit_pins(units_by_lib_id)
    parts: dict[str, list[tuple[int, list[dict]]]] = {}
    for refdes, comp in components.items():
        lib_id = comp.get("lib_symbol") or ""
        if ":" not in lib_id:
            continue
        units = units_by_lib_id.get(lib_id) or {1: []}
        parts[refdes] = [(u, units[u]) for u in sorted(units)]
    unplaced = set(((hdm.get("synthesis") or {}).get("placement") or {}).get("unplaced") or [])
    positions: dict[str, tuple[float, float]] = {}
    for refdes, comp in components.items():
        pl = comp.get("placement") or {}
        if refdes not in unplaced and isinstance(pl.get("x"), (int, float)) and isinstance(pl.get("y"), (int, float)):
            positions[refdes] = (float(pl["x"]), float(pl["y"]))
    # The refdes and what it is, short: a block is as wide as its title, and a
    # full catalogue description stretched one connector's block across half
    # the sheet. The description is on the symbol for anyone who wants it.
    def _title(r: str) -> str:
        what = str(components[r].get("mpn") or components[r].get("value") or "")
        title = f"{r} — {what}" if what and what != r else r
        return title if len(title) <= 48 else title[:47] + "…"

    titles = {r: _title(r) for r in parts}
    near = {r: c["near"] for r, c in components.items() if c.get("near")}
    blocks = sch_layout.assign_blocks(parts, pin_net_map, positions, titles, near)
    texts = {r: (r, str(components[r].get("value", r))) for r in parts}
    undriven = _undriven_power_nets(hdm, pin_net_map, pins_by_lib_id)
    # The PWR_FLAG row goes under the blocks; the packing leaves it room.
    flag_rows = math.ceil(len(undriven) / 12) if undriven else 0
    placed = sch_layout.layout(blocks, parts, pin_net_map, texts,
                               extra_height=(12 + 12 * flag_rows) * GRID_MM + 20 * GRID_MM)
    content_w = max((b.x1 for b in blocks), default=100.0)
    content_h = max((b.y1 for b in blocks), default=100.0)
    flag_row_y = _snap(content_h + 12 * GRID_MM)
    sheet_w = _snap(content_w + 20 * GRID_MM)
    sheet_h = _snap(flag_row_y + (16 * GRID_MM if undriven else 0) + 20 * GRID_MM)

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

    def _resolve(ref: str, seen: tuple[str, ...] = ()) -> sexpr.Sexp:
        """Load a symbol and flatten any ``(extends ...)`` chain above it.

        Bringing the parent along beside the child is not enough — see
        _flatten_derived for why KiCad cannot match the two up inside a
        schematic — so the chain is collapsed here and the result is
        self-contained.
        """
        if ref in seen:
            chain = " -> ".join((*seen, ref))
            raise loaders.LibraryMiss(f"circular symbol inheritance: {chain}")
        shell = loaders.load_symbol_shell(ref, symbols_root)
        parent_name = _extends_of(shell)
        if not parent_name:
            return shell
        lib, _, _ = ref.partition(":")
        parent = _resolve(f"{lib}:{parent_name}", (*seen, ref))
        return _flatten_derived(shell, parent)

    def _add(ref: str) -> None:
        """Emit one self-contained symbol into the cache."""
        if ref in emitted:
            return
        resolved = _resolve(ref)
        emitted.add(ref)
        lib_symbols_children.append(deepcopy(resolved))

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

    power_count = 0

    def terminate(net: str, tx: float, ty: float, d: int, *, labels_only: bool = False) -> list[sexpr.Node]:
        """End a pin: a stub, then a power symbol for a rail or ground, a
        global label for anything else."""
        nonlocal power_count
        sx, sy = sch_layout._STEP[d]
        ex, ey = _snap(tx + sx * sch_layout.STUB), _snap(ty + sy * sch_layout.STUB)
        out: list[sexpr.Node] = [_wire(tx, ty, ex, ey)]
        kind = sch_layout.net_kind(net)
        # A power symbol only where it reads the right way up: a rail's
        # standing up, ground's hanging down. Turned on its side it is as tall
        # as two pin pitches and its name runs across the neighbouring pins,
        # so a side pin takes a label of the same name (KiCad joins the two).
        if not sch_layout.ends_in_symbol(net, d, labels_only):
            out.append(_global_label(net, ex, ey, d))
            return out
        lib = POWER_GROUND_LIB_ID if kind == "ground" else POWER_RAIL_LIB_ID
        _add(lib)
        # GND's body hangs below its pin and VCC's stands above it; turn each
        # so the body continues the way the stub runs.
        body = 270 if kind == "ground" else 90
        power_count += 1
        out.append(_power_instance(lib, f"#PWR{power_count:03d}", net, ex, ey,
                                   (d - body) % 360, project_name, schematic_uuid))
        return out

    def run_terminals(run: list[tuple[str, float, float, int]], labels_only: bool = False) -> list[sexpr.Node]:
        """End a run of adjacent pins on one net, side by side on one edge.

        One pin ends as ``terminate`` ends it. Several are tied: a stub from
        each, a wire along their ends, and one terminal for the lot — ground
        hanging below the last, a rail standing on the first, a signal's label
        at the first. Five GND pins in a row used to be five ground symbols
        drawn on top of each other.
        """
        net = run[0][0]
        if len(run) == 1:
            _, tx, ty, d = run[0]
            return terminate(net, tx, ty, d, labels_only=labels_only)
        d = run[0][3]
        sx, sy = sch_layout._STEP[d]
        ends = [(_snap(tx + sx * sch_layout.STUB), _snap(ty + sy * sch_layout.STUB)) for _, tx, ty, _ in run]
        out: list[sexpr.Node] = [_wire(tx, ty, ex, ey) for (_, tx, ty, _), (ex, ey) in zip(run, ends)]
        out += [_wire(*a, *b) for a, b in zip(ends, ends[1:])]
        out += [_junction(*e) for e in ends[1:-1]]
        # The terminal goes outward from the first end, the way the pins
        # point. Hung below the run instead, a ground symbol's stub landed on
        # the next pin's stub 2.54 mm down and shorted RF_LRHF to GND.
        return out + terminate(net, *ends[0], d, labels_only=labels_only)

    for block in blocks:
        node.extend(_frame(block))

    for p in placed:
        comp = components[p.ref]
        lib_id = comp["lib_symbol"]
        placeholder = bool(comp.get("needs_manual_symbol"))
        nets = pin_net_map.get(p.ref) or {}
        x0, y0, x1, y1 = sch_layout.part_extent(p.pins, p.rot, nets)
        tips = [sch_layout.pin_tip(pin, p.x, p.y, p.rot) for pin in p.pins]
        if len(p.pins) > 2 or not tips:
            ref_at = (p.x, _snap(p.y + y0 - GRID_MM), None)
            value_at = (p.x, _snap(p.y + y1 + 2 * GRID_MM), None)
        elif len({t[0] for t in tips}) == 1:
            # Standing up: the text sits beside the body. KiCad mirrors a
            # field's justification with a symbol turned 180°, so it is given
            # mirrored to come out left-aligned clear of the body.
            just = "right" if p.rot == 180 else "left"
            ref_at = (_snap(p.x + 2 * GRID_MM), _snap(p.y - GRID_MM), just)
            value_at = (_snap(p.x + 2 * GRID_MM), _snap(p.y + GRID_MM), just)
        else:
            ref_at = (p.x, _snap(p.y - 3 * GRID_MM), None)
            value_at = (p.x, _snap(p.y + 3 * GRID_MM), None)
        node.append(
            _component_instance(
                refdes=p.ref,
                lib_id=lib_id,
                value=str(comp.get("value", p.ref)),
                footprint=str(comp.get("footprint", "")),
                x=p.x,
                y=p.y,
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
                unit=p.unit,
                rot=p.rot,
                ref_at=ref_at,
                value_at=value_at,
            )
        )
        # Physical pin numbers the design declares not-connected (Stage 5 writes
        # them as ``no_connect_pins``). A pin that is both NC and on a net is a
        # contradiction upstream; the net wins here, because a wire that exists
        # is the safer thing to show.
        nc_pins = {str(n) for n in (comp.get("no_connect_pins") or [])}
        for pin, (tx, ty, d) in zip(p.pins, tips):
            if not nets.get(pin["number"]) and pin["number"] in nc_pins:
                node.append(_no_connect(tx, ty))
        # An IC's pins sit one pitch apart, and a power symbol's name is wider
        # than that: on an anchor every pin ends in a label. Power symbols are
        # for the small parts, where each stands clear of the next.
        for run in _same_net_runs(p.pins, tips, nets):
            node.extend(run_terminals(run, labels_only=len(p.pins) > 2))

    # PWR_FLAGs, in a row under the blocks: each declares its rail driven, and
    # carries the rail's own power symbol (or label) so it joins that net.
    if undriven:
        _add(PWR_FLAG_LIB_ID)
        for i, net_name in enumerate(undriven):
            fx = _snap(20 * GRID_MM + (i % 12) * 16 * GRID_MM)
            fy = _snap(flag_row_y + (i // 12) * 12 * GRID_MM)
            node.append(
                _pwr_flag_instance(i + 1, fx, fy, project_name, schematic_uuid, net_name)
            )
            node.extend(terminate(net_name, fx, fy, 270))

    node.append(["sheet_instances", ["path", '"/"', ["page", '"1"']]])
    node.append(["embedded_fonts", "no"])
    return node


def flagged_nets(hdm: dict, *, symbols_root: Path) -> list[str]:
    """The nets this emitter puts a PWR_FLAG on. Recorded so Stage 8 can tell an
    unsourced rail apart from one whose source it simply cannot see."""
    pin_net_map = _build_component_pin_net_map(hdm)
    pins_by_lib_id = _all_unit_pins(_load_units(hdm.get("components") or {}, symbols_root))
    return _undriven_power_nets(hdm, pin_net_map, pins_by_lib_id)


def emit(hdm: dict, *, symbols_root: Path) -> str:
    return sexpr.dump_top(build(hdm, symbols_root=symbols_root))


def write(hdm: dict, output_path: Path, *, symbols_root: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(emit(hdm, symbols_root=symbols_root), encoding="utf-8")
    return output_path
