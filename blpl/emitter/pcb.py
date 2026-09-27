"""v10 ``.kicad_pcb`` emitter.

Produces a board file that kicad-cli 9.x / 10.x can load for DRC. Each component
becomes a ``(footprint ...)`` block whose pad geometry is copied from the
referenced library footprint in kicad-footprints/, with nets overridden to match
our HDM nets.

Unlike the legacy ``yaml_to_kicad.py``, pads are not faked at ``(at 0 0) (size 1 1)``;
they carry the real library geometry so DRC results are meaningful.
"""

from __future__ import annotations

import math
import uuid as _uuid
from copy import deepcopy
from collections.abc import Sequence
from pathlib import Path

from . import loaders, sexpr


_PCB_VERSION = "20260206"
_GENERATOR_VERSION = "10.0"

# A4 landscape = 297×210mm. KiCad's rendered working area sits inside a border:
# usable rectangle 12.5,12.5 → 284.5,197.5, with a notch carved out for the
# title-block at 176.5,165.5 → 284.5,197.5. We offset every component and the
# board outline by (_ORIGIN_X, _ORIGIN_Y) so HDM (0,0) corresponds to the
# top-left of the usable area.
_ORIGIN_X = 12.5
_ORIGIN_Y = 12.5
_SHEET_USABLE_MAX_X = 284.5
_SHEET_USABLE_MAX_Y = 197.5
# Notch (title-block cutout) in absolute sheet coordinates.
_NOTCH_MIN_X = 176.5
_NOTCH_MIN_Y = 165.5
_NOTCH_MAX_X = 284.5
_NOTCH_MAX_Y = 197.5
_EDGE_CUTS_WIDTH = 0.05


# v10 layer table (IDs differ from v6). Copied from a KiCad-10-saved reference.
_DEFAULT_LAYERS: list[sexpr.Sexp] = [
    ["0", '"F.Cu"', "signal"],
    ["2", '"B.Cu"', "signal"],
    ["9", '"F.Adhes"', "user", '"F.Adhesive"'],
    ["11", '"B.Adhes"', "user", '"B.Adhesive"'],
    ["13", '"F.Paste"', "user"],
    ["15", '"B.Paste"', "user"],
    ["5", '"F.SilkS"', "user", '"F.Silkscreen"'],
    ["7", '"B.SilkS"', "user", '"B.Silkscreen"'],
    ["1", '"F.Mask"', "user"],
    ["3", '"B.Mask"', "user"],
    ["17", '"Dwgs.User"', "user", '"User.Drawings"'],
    ["19", '"Cmts.User"', "user", '"User.Comments"'],
    ["21", '"Eco1.User"', "user", '"User.Eco1"'],
    ["23", '"Eco2.User"', "user", '"User.Eco2"'],
    ["25", '"Edge.Cuts"', "user"],
    ["27", '"Margin"', "user"],
    ["31", '"F.CrtYd"', "user", '"F.Courtyard"'],
    ["29", '"B.CrtYd"', "user", '"B.Courtyard"'],
    ["35", '"F.Fab"', "user"],
    ["33", '"B.Fab"', "user"],
]


def _new_uuid() -> str:
    return f'"{_uuid.uuid4()}"'


def _resolve_pin_map(hdm_components: dict, refdes: str) -> dict[str, str]:
    """Return {logical_pin: physical_pin} for a component, empty if none declared."""
    comp = hdm_components.get(refdes, {})
    return {str(k): str(v) for k, v in (comp.get("pin_map") or {}).items()}


def _build_pad_nets(hdm: dict) -> tuple[dict[tuple[str, str], tuple[int, str]], list[tuple[int, str]]]:
    """From the HDM, build:

      - ``(refdes, physical_pin) -> (net_ordinal, net_name)`` for pad-level net injection
      - ``[(net_ordinal, net_name), ...]`` for emitting (net N "NAME") block (0 reserved)
    """
    components = hdm.get("components", {})
    nets = hdm.get("nets", {})

    pad_to_net: dict[tuple[str, str], tuple[int, str]] = {}
    net_table: list[tuple[int, str]] = [(0, "")]

    for ordinal, (net_name, net_def) in enumerate(nets.items(), start=1):
        net_table.append((ordinal, net_name))
        pin_maps_by_ref: dict[str, dict[str, str]] = {}
        for pair in net_def.get("pads", []):
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            refdes, logical_pin = str(pair[0]), str(pair[1])
            pin_map = pin_maps_by_ref.setdefault(refdes, _resolve_pin_map(components, refdes))
            physical = pin_map.get(logical_pin, logical_pin)
            pad_to_net[(refdes, physical)] = (ordinal, net_name)

    return pad_to_net, net_table


def _override_pad_net(pad_node: sexpr.Node, ordinal: int, net_name: str) -> None:
    """Add or replace the ``(net N "NAME")`` inside a pad node in-place."""
    existing = sexpr.find(pad_node, "net")
    new_net_node = ["net", str(ordinal), sexpr.quote(net_name)]
    if existing is not None:
        existing[:] = new_net_node
    else:
        # Insert before uuid if present, else append.
        uuid_node = sexpr.find(pad_node, "uuid")
        if uuid_node is not None:
            idx = pad_node.index(uuid_node)
            pad_node.insert(idx, new_net_node)
        else:
            pad_node.append(new_net_node)


def _override_footprint_position(footprint_node: sexpr.Node, x: float, y: float, rot: float) -> None:
    at_node = sexpr.find(footprint_node, "at")
    new_at: sexpr.Node = ["at", f"{x}", f"{y}"]
    if rot:
        new_at.append(f"{rot}")
    if at_node is not None:
        at_node[:] = new_at
    else:
        # Insert near the top (after layer if present).
        insert_idx = 1
        for i, child in enumerate(footprint_node[1:], start=1):
            if isinstance(child, list) and sexpr.head(child) in ("layer", "uuid"):
                insert_idx = i + 1
        footprint_node.insert(insert_idx, new_at)


def _override_footprint_layer(footprint_node: sexpr.Node, side: str) -> None:
    target = '"B.Cu"' if side.lower() == "bottom" else '"F.Cu"'
    layer_node = sexpr.find(footprint_node, "layer")
    if layer_node is not None:
        layer_node[:] = ["layer", target]


# Legacy footprint upgrade --------------------------------------------------
#
# Footprints reach the emitter from wherever Stage 5 found them, and the
# vendor/module libraries are whatever a part's maker or SnapEDA/UltraLibrarian
# exported: KiCad 5 ``(module ...)`` files with ``(tedit ...)``, arcs written as
# centre + start + sweep angle, ``(width w)`` strokes, and ``(fp_text reference
# "REF**")`` in place of a Reference property. KiCad itself upgrades those when
# it *loads a library*; it does not upgrade them when it finds them embedded in
# a v10 board file. So the emitter copied them in verbatim and produced a board
# kicad-cli refused outright ("Expecting 'mid'" at the first legacy arc) — and
# where it did load, seven parts kept the reference ``REF**`` because only
# ``(property "Reference" ...)`` was ever patched, so the schematic/PCB
# cross-check reported them missing from the board.
#
# The upgrade is done here, in Python, rather than by shelling out to
# ``kicad-cli fp upgrade``: the emitter's contract is to run without KiCad
# installed, and rewriting a user's library in place is not the emitter's call.

_LEGACY_SHAPE_TAGS = ("fp_line", "fp_arc", "fp_circle", "fp_rect", "fp_poly")


def _num(atom: str) -> float:
    return float(sexpr.unquote(atom))


def _fmt(v: float) -> str:
    """Render a coordinate the way KiCad writes one: no trailing zeros, no -0."""
    r = round(v, 6)
    if r == 0:
        r = 0.0
    text = f"{r:.6f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _rotate_std(x: float, y: float, degrees: float) -> tuple[float, float]:
    """Rotate (x, y) about the origin by ``degrees`` on raw coordinates."""
    theta = math.radians(degrees)
    c, s = math.cos(theta), math.sin(theta)
    return (x * c - y * s, x * s + y * c)


def _upgrade_legacy_arc(arc: sexpr.Node) -> None:
    """Rewrite a centre/start/angle arc as the start/mid/end form, in place.

    KiCad 5 wrote an arc as ``(start CENTRE) (end STARTPOINT) (angle SWEEP)``.
    Its own parser, on reading that form, computes the end point by rotating
    the start point about the centre by ``-angle`` with ``RotatePoint`` — whose
    convention, on raw y-down coordinates, is a standard rotation by ``+angle``
    — and then, when the angle is negative, swaps start and end so the arc is
    always stored counter-clockwise. The mid point sits half-way round, and
    does not care which way the ends were swapped.
    """
    start_node = sexpr.find(arc, "start")
    end_node = sexpr.find(arc, "end")
    angle_node = sexpr.find(arc, "angle")
    if start_node is None or end_node is None or angle_node is None:
        return
    cx, cy = _num(start_node[1]), _num(start_node[2])
    sx, sy = _num(end_node[1]), _num(end_node[2])
    sweep = _num(angle_node[1])
    rx, ry = sx - cx, sy - cy
    ex, ey = _rotate_std(rx, ry, sweep)
    mx, my = _rotate_std(rx, ry, sweep / 2.0)
    p_start = (sx, sy)
    p_end = (cx + ex, cy + ey)
    if sweep < 0:
        p_start, p_end = p_end, p_start
    start_node[:] = ["start", _fmt(p_start[0]), _fmt(p_start[1])]
    end_node[:] = ["end", _fmt(p_end[0]), _fmt(p_end[1])]
    arc.insert(arc.index(end_node), ["mid", _fmt(cx + mx), _fmt(cy + my)])
    arc.remove(angle_node)


def _upgrade_legacy_stroke(shape: sexpr.Node) -> None:
    """``(width w)`` → ``(stroke (width w) (type solid))`` when no stroke exists."""
    if sexpr.find(shape, "stroke") is not None:
        return
    width_node = sexpr.find(shape, "width")
    if width_node is None:
        return
    shape[shape.index(width_node)] = ["stroke", ["width", width_node[1]], ["type", "solid"]]


def _fp_text_to_property(text_node: sexpr.Node, name: str) -> sexpr.Node:
    """Turn ``(fp_text reference|value X (at ..) (layer ..) [hide] (effects ..))``
    into the v10 ``(property "Name" "X" ...)`` field that carries it now."""
    prop: sexpr.Node = ["property", sexpr.quote(name), text_node[2]]
    hidden = False
    for child in text_node[3:]:
        if child == "hide":
            hidden = True
            continue
        if isinstance(child, list) and sexpr.head(child) == "hide":
            hidden = True
            continue
        if isinstance(child, list) and sexpr.head(child) == "at":
            # KiCad 6 wrote ``(at x y unlocked)``; unlocked is its own node now.
            at = [a for a in child if a != "unlocked"]
            prop.append(at)
            if "unlocked" in child:
                prop.append(["unlocked", "yes"])
            continue
        prop.append(child)
    if hidden:
        prop.append(["hide", "yes"])
    return prop


def _drop_degenerate_lines(fp_node: sexpr.Node) -> int:
    """Remove ``fp_line`` segments whose start and end are the same point.

    A zero-length line has no geometry, so dropping one cannot change a shape —
    but KiCad reports each as ``malformed_courtyard``, and a courtyard it calls
    malformed is worse than noisy. KiCad exports **no outline at all** for such
    a footprint into Specctra, so the autorouter gets no keep-out for the part
    and routes through the space it occupies. On sb-halow that was the largest
    part on the board, and the resulting "0 unrouted" was reached by routing
    under a module.

    Done here rather than by editing libraries because the same defect is in
    stock kicad-footprints — SOT-723, Telit_xL865, nRF24L01_Breakout and three
    pressure sensors all carry one — and those are an upstream submodule this
    project does not own. Cleaning them on the way into the board fixes every
    footprint, whoever wrote it.
    """
    dropped = 0
    for line in list(sexpr.find_all(fp_node, "fp_line")):
        start, end = sexpr.find(line, "start"), sexpr.find(line, "end")
        if start is None or end is None:
            continue
        try:
            same = (float(start[1]), float(start[2])) == (float(end[1]), float(end[2]))
        except (IndexError, TypeError, ValueError):
            continue
        if same:
            fp_node.remove(line)
            dropped += 1
    return dropped


def _upgrade_legacy_footprint(fp_node: sexpr.Node) -> None:
    """Bring a pre-v10 footprint up to what a v10 board file may contain.

    Idempotent on a modern footprint: nothing here matches, nothing changes.
    """
    if fp_node and fp_node[0] == "module":
        fp_node[0] = "footprint"

    for tedit in sexpr.find_all(fp_node, "tedit"):
        fp_node.remove(tedit)

    for text_node in sexpr.find_all(fp_node, "fp_text"):
        kind = text_node[1] if len(text_node) > 2 else None
        if kind == "reference":
            fp_node[fp_node.index(text_node)] = _fp_text_to_property(text_node, "Reference")
        elif kind == "value":
            fp_node[fp_node.index(text_node)] = _fp_text_to_property(text_node, "Value")

    _drop_degenerate_lines(fp_node)

    for tag in _LEGACY_SHAPE_TAGS:
        for shape in sexpr.find_all(fp_node, tag):
            if tag == "fp_arc" and sexpr.find(shape, "mid") is None:
                _upgrade_legacy_arc(shape)
            _upgrade_legacy_stroke(shape)


#: KiCad's stroke font advances roughly 0.78 of the glyph height per character
#: at default thickness. Used to ask whether a reference will physically fit on
#: the part it names, rather than guessing from the package name.
_CHAR_ADVANCE = 0.78

#: The board's silkscreen minimum, which is KiCad's own default and the floor
#: any reference has to clear. Text cannot simply be shrunk to fit a 0402.
_MIN_SILK_TEXT_MM = 0.8


def _courtyard_extent(footprint_node: sexpr.Node) -> tuple[float, float] | None:
    """(width, height) of the courtyard, or None when there is none to read.

    A circle gives a centre and a point on the circumference, not two opposite
    corners, so it is expanded to centre +/- radius. Read as corners it collapses
    the box to a line — a round courtyard measures as having no height — which
    is the same mistake `placement.py` documents having made, and repeating it
    here would hide the silkscreen of every circular footprint on the board.
    """
    xs: list[float] = []
    ys: list[float] = []
    for tag in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc"):
        for shape in sexpr.find_all(footprint_node, tag):
            layer = sexpr.find(shape, "layer")
            if layer is None or "CrtYd" not in sexpr.unquote(str(layer[1])):
                continue
            pts: list[tuple[float, float]] = []
            for key in ("start", "end", "center", "mid"):
                for node in sexpr.find_all(shape, key):
                    try:
                        pts.append((float(node[1]), float(node[2])))
                    except (IndexError, TypeError, ValueError):
                        pass
            for group in sexpr.find_all(shape, "pts"):
                for xy in sexpr.find_all(group, "xy"):
                    try:
                        pts.append((float(xy[1]), float(xy[2])))
                    except (IndexError, TypeError, ValueError):
                        pass
            if tag == "fp_circle" and len(pts) >= 2:
                (cx, cy), (ex, ey) = pts[0], pts[1]
                r = math.hypot(ex - cx, ey - cy)
                pts = [(cx - r, cy - r), (cx + r, cy + r)]
            for x, y in pts:
                xs.append(x); ys.append(y)
    if not xs or not ys:
        return None
    return (max(xs) - min(xs), max(ys) - min(ys))


def _reference_font_mm(prop: sexpr.Node) -> float:
    """The height the reference will actually be emitted at.

    Not the board minimum. Vendor footprints in this project carry 1.0 mm and
    1.27 mm reference fonts, and estimating a 0.8 mm label for text that will
    be emitted at 1.27 mm accepts a reference half again as wide as the space
    measured for it.
    """
    for effects in sexpr.find_all(prop, "effects"):
        for font in sexpr.find_all(effects, "font"):
            for size in sexpr.find_all(font, "size"):
                try:
                    return max(float(size[2]), float(size[1]))
                except (IndexError, TypeError, ValueError):
                    pass
    return _MIN_SILK_TEXT_MM


def _reference_is_rotated(prop: sexpr.Node) -> bool:
    """Whether the reference text runs across the footprint rather than along it.

    Rotating a footprint turns its courtyard and its reference together, so the
    two keep the same relative orientation and "try both ways round" is not a
    test of anything. What decides which courtyard axis the text has to fit in
    is the angle on the property itself.
    """
    at = sexpr.find(prop, "at")
    if at is None or len(at) < 4:
        return False
    try:
        return round(float(at[3]) / 90.0) % 2 == 1
    except (TypeError, ValueError):
        return False


def _silk_reference_fits(
    footprint_node: sexpr.Node, refdes: str, prop: sexpr.Node
) -> bool:
    """Whether this part is big enough to carry its own reference in silk.

    A 0402 is 1.0 x 0.5 mm and `C_U1_VDD1` at the 0.8 mm silkscreen minimum is
    about 6 mm wide. The text cannot shrink — 0.8 mm is the board's floor and
    KiCad's default — so it lands on its neighbours and on copper instead. That
    is most of what makes a dense board look unreadable: sb-halow reported 21
    silk_overlap and 10 silk_over_copper across eleven parts.

    The reference is not lost by hiding it. Stock footprints carry a
    ``${REFERENCE}`` on F.Fab, which is the assembly drawing — where a
    placement operator reads it from anyway.

    Measured against the courtyard rather than decided from the refdes prefix:
    "R" says nothing about size, and a part with no courtyard to measure keeps
    its silk rather than being hidden on a guess.
    """
    extent = _courtyard_extent(footprint_node)
    if extent is None:
        return True
    height = _reference_font_mm(prop)
    width_needed = len(refdes) * _CHAR_ADVANCE * height
    along, across = extent
    if _reference_is_rotated(prop):
        along, across = across, along
    return width_needed <= along and height <= across


def _hide(prop: sexpr.Node) -> None:
    """Mark a property hidden, whether or not it already says otherwise."""
    for node in sexpr.find_all(prop, "hide"):
        node[:] = ["hide", "yes"]
        return
    prop.append(["hide", "yes"])


def _set_reference_and_value(
    footprint_node: sexpr.Node, refdes: str, value: str
) -> None:
    for prop in sexpr.find_all(footprint_node, "property"):
        # property structure: ["property", '"Reference"', '"OLD"', ...]
        if len(prop) >= 3 and isinstance(prop[1], str):
            pname = sexpr.unquote(prop[1])
            if pname == "Reference":
                prop[2] = sexpr.quote(refdes)
                layer = sexpr.find(prop, "layer")
                on_silk = layer is not None and "SilkS" in sexpr.unquote(str(layer[1]))
                # Decided per property, because the answer depends on that
                # property's own font size and angle — not on the footprint
                # alone.
                if on_silk and not _silk_reference_fits(footprint_node, refdes, prop):
                    _hide(prop)
            elif pname == "Value":
                prop[2] = sexpr.quote(value)


def _replace_uuid(node: sexpr.Node) -> None:
    """Regenerate the top-level ``(uuid ...)`` of a node (used for footprints and pads)."""
    uuid_node = sexpr.find(node, "uuid")
    if uuid_node is not None:
        uuid_node[:] = ["uuid", _new_uuid()]
    else:
        # Append at the end.
        node.append(["uuid", _new_uuid()])


def _board_dimensions(hdm: dict) -> tuple[float, float]:
    """Return (width, height) mm from project.dimensions, defaulting to A4 usable."""
    dims = hdm.get("project", {}).get("dimensions") or []
    if isinstance(dims, (list, tuple)) and len(dims) >= 2:
        try:
            return float(dims[0]), float(dims[1])
        except (TypeError, ValueError):
            pass
    return 100.0, 80.0


def _edge_cuts_rect(width: float, height: float) -> list[sexpr.Node]:
    """Four ``(gr_line ...)`` segments outlining a W×H rectangle at the sheet origin.

    The outline sits at (_ORIGIN_X, _ORIGIN_Y) → (+width, +height) so the board's
    (0,0) local origin lines up with the top-left of the KiCad working area.
    """
    x0, y0 = _ORIGIN_X, _ORIGIN_Y
    x1, y1 = x0 + width, y0 + height
    corners = [
        ((x0, y0), (x1, y0)),  # top
        ((x1, y0), (x1, y1)),  # right
        ((x1, y1), (x0, y1)),  # bottom
        ((x0, y1), (x0, y0)),  # left
    ]
    segments: list[sexpr.Node] = []
    for (sx, sy), (ex, ey) in corners:
        segments.append(
            [
                "gr_line",
                ["start", f"{sx}", f"{sy}"],
                ["end", f"{ex}", f"{ey}"],
                ["stroke", ["width", f"{_EDGE_CUTS_WIDTH}"], ["type", "solid"]],
                ["layer", '"Edge.Cuts"'],
                ["uuid", _new_uuid()],
            ]
        )
    return segments


def _in_notch(sheet_x: float, sheet_y: float) -> bool:
    """True when (sheet_x, sheet_y) falls inside the title-block cutout."""
    return (
        _NOTCH_MIN_X <= sheet_x <= _NOTCH_MAX_X
        and _NOTCH_MIN_Y <= sheet_y <= _NOTCH_MAX_Y
    )


def _auto_grid_position(
    index: int, width: float, height: float, pitch: float = 10.0
) -> tuple[float, float]:
    """Grid-place the Nth footprint inside the usable board area, skipping the notch.

    Returns board-local (x, y) coordinates (pre-offset); the caller adds _ORIGIN_*.
    The grid steps left→right, top→bottom, clamped to the board dims; slots
    whose sheet-absolute position falls inside the notch are skipped, so the
    caller sees only notch-free positions.
    """
    cols = max(1, int(width // pitch))
    rows = max(1, int(height // pitch))
    scanned = 0
    n = 0
    while scanned < cols * rows:
        col = n % cols
        row = (n // cols) % rows
        x = col * pitch
        y = row * pitch
        sheet_x = x + _ORIGIN_X
        sheet_y = y + _ORIGIN_Y
        if not _in_notch(sheet_x, sheet_y):
            if scanned == index:
                return x, y
            scanned += 1
        n += 1
        if n > cols * rows * 2:
            break  # safety: more requests than slots, wrap back to origin.
    return 0.0, 0.0


def build(
    hdm: dict,
    *,
    footprints_root: Path | str | Sequence[Path | str],
) -> sexpr.Node:
    """Build a v10 (kicad_pcb ...) node from an HDM dict."""
    project = hdm.get("project", {})
    thickness = project.get("stackup", {}).get("thickness", 1.6)
    components = hdm.get("components", {})
    nets = hdm.get("nets", {})

    pad_to_net, net_table = _build_pad_nets(hdm)
    width, height = _board_dimensions(hdm)

    node: sexpr.Node = [
        "kicad_pcb",
        ["version", _PCB_VERSION],
        ["generator", '"blpl.emitter"'],
        ["generator_version", f'"{_GENERATOR_VERSION}"'],
        ["general", ["thickness", f"{thickness}"], ["legacy_teardrops", "no"]],
        ["paper", '"A4"'],
        ["layers", *_DEFAULT_LAYERS],
        _default_setup(),
    ]

    # Nets.
    for ordinal, name in net_table:
        node.append(["net", str(ordinal), sexpr.quote(name)])

    # Board outline on Edge.Cuts.
    for seg in _edge_cuts_rect(width, height):
        node.append(seg)

    # Footprints. A component with no loadable footprint used to be skipped
    # silently, so the board simply came out missing parts and nothing said so.
    # Stage 5 now resolves every footprint against the real libraries and
    # substitutes a placeholder when it can't, so reaching here with an
    # unloadable reference means the pipeline is broken, not the design.
    auto_index = 0
    for refdes, comp in components.items():
        fp_ref = comp.get("footprint")
        if not fp_ref or ":" not in fp_ref:
            raise loaders.LibraryMiss(
                f"{refdes} has no footprint reference ({fp_ref!r}). Stage 5 should have "
                "substituted a placeholder; run stage5 with a footprint library root."
            )
        fp_node = loaders.load_footprint(fp_ref, footprints_root)
        fp_node = deepcopy(fp_node)  # don't mutate the library copy
        _upgrade_legacy_footprint(fp_node)

        # Library .kicad_mod files identify themselves by bare footprint name; a
        # PCB expects the "Lib:Name" qualified form so KiCad can trace it back
        # to its source library.
        if len(fp_node) >= 2 and isinstance(fp_node[1], str):
            fp_node[1] = sexpr.quote(fp_ref)

        placement = comp.get("placement", {}) or {}
        raw_x = placement.get("x")
        raw_y = placement.get("y")
        rot = float(placement.get("rot", 0))
        side = str(placement.get("side", "top"))

        if raw_x is None or raw_y is None:
            bx, by = _auto_grid_position(auto_index, width, height)
            auto_index += 1
        else:
            bx = float(raw_x)
            by = float(raw_y)

        # Translate board-local coordinates into sheet coordinates.
        sheet_x = bx + _ORIGIN_X
        sheet_y = by + _ORIGIN_Y

        _override_footprint_layer(fp_node, side)
        _override_footprint_position(fp_node, sheet_x, sheet_y, rot)
        _set_reference_and_value(fp_node, refdes, str(comp.get("value", refdes)))
        _replace_uuid(fp_node)

        if comp.get("needs_manual_footprint"):
            # The land pattern under this part is a stand-in, not the real one.
            # Say so on the footprint itself, so it survives being opened in
            # KiCad by someone who never read the report.
            fp_node.append(
                [
                    "property",
                    '"BLPL_PLACEHOLDER"',
                    sexpr.quote(
                        f"NOT THE REAL FOOTPRINT — wanted "
                        f"{comp.get('requested_footprint', '(none)')}"
                    ),
                    ["at", "0", "0", "0"],
                    ["unlocked", "yes"],
                    ["layer", '"F.Fab"'],
                    ["hide", "yes"],
                    ["uuid", _new_uuid()],
                    ["effects", ["font", ["size", "1", "1"], ["thickness", "0.15"]]],
                ]
            )

        for pad_node in loaders.extract_pads(fp_node):
            if len(pad_node) < 2 or not isinstance(pad_node[1], str):
                continue
            pad_number = sexpr.unquote(pad_node[1])
            key = (refdes, pad_number)
            if key in pad_to_net:
                ordinal, net_name = pad_to_net[key]
                _override_pad_net(pad_node, ordinal, net_name)
            _replace_uuid(pad_node)

        node.append(fp_node)

    placeholders = sorted(r for r, c in components.items() if c.get("needs_manual_footprint"))
    if placeholders:
        node.append(_placeholder_banner(placeholders, width, height))

    return node


def _placeholder_banner(refdes_list: list[str], width: float, height: float) -> sexpr.Node:
    """A warning printed on the board's silkscreen, above the outline.

    Deliberately on F.SilkS rather than a comment layer: a placeholder footprint is
    the wrong land pattern in copper, and this has to be visible in every render and
    every fab preview, not just to someone who thought to turn a layer on.
    """
    shown = ", ".join(refdes_list[:12]) + (" …" if len(refdes_list) > 12 else "")
    return [
        "gr_text",
        sexpr.quote(
            f"*** {len(refdes_list)} PLACEHOLDER FOOTPRINT(S) — DO NOT FABRICATE ***\n"
            f"{shown}\n"
            "These land patterns are 2.54mm headers standing in for parts whose real "
            "footprints do not exist.\n"
            "Draw them into libraries/footprints/, then re-run stage5."
        ),
        ["at", f"{_ORIGIN_X:.3f}", f"{_ORIGIN_Y - 8.0:.3f}", "0"],
        ["layer", '"F.SilkS"'],
        ["uuid", _new_uuid()],
        ["effects", ["font", ["size", "2", "2"], ["thickness", "0.3"], ["bold", "yes"]], ["justify", "left"]],
    ]


def _default_setup() -> sexpr.Node:
    """Minimal v10 (setup ...) block. KiCad fills in defaults for anything we omit."""
    return [
        "setup",
        ["pad_to_mask_clearance", "0"],
        ["allow_soldermask_bridges_in_footprints", "no"],
        ["tenting", ["front", "yes"], ["back", "yes"]],
    ]


def emit(hdm: dict, *, footprints_root: Path | str | Sequence[Path | str]) -> str:
    """Render a full .kicad_pcb file content from an HDM."""
    return sexpr.dump_top(build(hdm, footprints_root=footprints_root))


def write(hdm: dict, output_path: Path, *, footprints_root: Path | str | Sequence[Path | str]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(emit(hdm, footprints_root=footprints_root), encoding="utf-8")
    return output_path
