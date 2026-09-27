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
    unit: int = 1,
) -> sexpr.Node:
    return [
        "symbol",
        ["lib_id", sexpr.quote(lib_id)],
        ["at", _mm(x), _mm(y), "0"],
        ["unit", str(unit)],
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
    for i, key in enumerate(components):
        col, row = i % cols, i // cols
        layout.append(
            (key, _snap(origin_x + col * cell_w), _snap(origin_y + row * cell_h))
        )
    return layout


def _placements(components: dict, units_by_lib_id: dict[str, dict[int, list[dict]]]) -> list[tuple[str, int]]:
    """Every (refdes, unit) that gets its own symbol instance and grid cell.

    A multi-unit symbol is several bodies sharing one reference, and KiCad
    wants each body placed separately. Counting components rather than units
    sized the sheet for one body per part and drew exactly that — so a
    216-pin MCU appeared as its 64-pin unit 1 and nothing else.
    """
    out: list[tuple[str, int]] = []
    for refdes, comp in components.items():
        lib_id = comp.get("lib_symbol") or ""
        units = units_by_lib_id.get(lib_id) or {1: []}
        for unit in sorted(units):
            out.append((refdes, unit))
    return out


def _reading_order(
    placements: list[tuple[str, int]], components: dict, bands: int | None = None
) -> list[tuple[str, int]]:
    """Order symbols so parts that belong together land together on the sheet.

    The grid used to follow BOM order, which is the order parts happen to
    appear in the design document. A decoupling capacitor could sit a dozen
    cells from the pin it decouples, and reviewing the sheet meant chasing
    labels across it — every connection here is a net label, so there is no
    wire to follow with your eye. "A grid of symbols" was a fair description.

    Stage 5 has already solved the grouping problem for the board:
    ``placement.py`` clusters by net adjacency with the global rails excluded,
    and anchors each bypass capacitor to the IC it is named for. Reusing those
    coordinates costs nothing and means the schematic and the board agree about
    what is near what, which is its own help when reading one beside the other.

    Ordering is a horizontal sweep through bands of the board — reading order —
    rather than raw ``(y, x)``, so a part a millimetre lower than its
    neighbour does not get pushed to a different row of the sheet.

    Components with no placement keep their original relative order and go
    last, so an unplaced part is never silently dropped or reshuffled.
    """
    pos: dict[str, tuple[float, float]] = {}
    for refdes, comp in components.items():
        p = comp.get("placement") or {}
        x, y = p.get("x"), p.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            pos[refdes] = (float(x), float(y))
    if not pos:
        return list(placements)

    ys = [y for _, y in pos.values()]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0
    n = bands or max(1, round(len(pos) ** 0.5))
    original = {item: i for i, item in enumerate(placements)}

    def key(item: tuple[str, int]) -> tuple:
        refdes, unit = item
        if refdes not in pos:
            return (1, 0, 0.0, original[item])
        x, y = pos[refdes]
        # Clamped so the part at exactly `hi` lands in the last band rather
        # than one past it.
        band = min(n - 1, int((y - lo) / span * n))
        return (0, band, x, original[item])

    return sorted(placements, key=key)


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
    # whose pins are unreachable.
    cell_w, cell_h = _cell_size(hdm, symbols_root)
    units_by_lib_id = _load_units(components, symbols_root)
    placements = _placements(components, units_by_lib_id)
    n = max(1, len(placements))
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

    # Component instances + per-pin label stubs for declared nets.
    pin_net_map = _build_component_pin_net_map(hdm)
    pins_by_lib_id = _all_unit_pins(units_by_lib_id)

    stub_len = 2.54  # one grid unit; enough to clear the symbol body.
    ordered = _reading_order(placements, hdm.get("components", {}) or {})
    for (refdes, unit), x, y in _component_grid(ordered, cell_w, cell_h, cols):
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
                unit=unit,
            )
        )

        component_nets = pin_net_map.get(refdes) or {}
        # Physical pin numbers the design declares not-connected (Stage 5 writes
        # them as ``no_connect_pins``). A pin that is both NC and on a net is a
        # contradiction upstream; the net wins here, because a wire that exists
        # is the safer thing to show.
        nc_pins = {str(p) for p in (comp.get("no_connect_pins") or [])}
        if not component_nets and not nc_pins:
            continue
        for pin in units_by_lib_id.get(lib_id, {}).get(unit, []):
            net_name = component_nets.get(pin["number"])
            tip_x, tip_y = _pin_absolute_tip(x, y, pin)
            if not net_name:
                if pin["number"] in nc_pins:
                    node.append(_no_connect(_snap(tip_x), _snap(tip_y)))
                continue
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
    pins_by_lib_id = _all_unit_pins(_load_units(hdm.get("components") or {}, symbols_root))
    return _undriven_power_nets(hdm, pin_net_map, pins_by_lib_id)


def emit(hdm: dict, *, symbols_root: Path) -> str:
    return sexpr.dump_top(build(hdm, symbols_root=symbols_root))


def write(hdm: dict, output_path: Path, *, symbols_root: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(emit(hdm, symbols_root=symbols_root), encoding="utf-8")
    return output_path
