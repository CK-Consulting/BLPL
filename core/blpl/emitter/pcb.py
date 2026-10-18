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
# Copper is not in it: how many copper layers a board has is the stackup's to
# say, and a fixed F.Cu/B.Cu table emitted every 4- and 6-layer design as a
# 2-layer board. _copper_layers builds that part.
_NON_COPPER_LAYERS: list[sexpr.Sexp] = [
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


# The counts schemas/project_config.v1.json allows.
_COPPER_COUNTS = frozenset({1, 2, 4, 6, 8, 10, 12})


def _copper_layers(count: int) -> list[sexpr.Sexp]:
    """The copper rows of the layer table for a ``count``-layer stackup.

    KiCad 10 numbers copper F.Cu = 0, B.Cu = 2 and In<n>.Cu = 2n + 2, and lists
    the inner layers between F.Cu and B.Cu — checked against boards pcbnew
    saved after ``SetCopperLayerCount``. A 1-layer board is written with B.Cu
    still in the table, because that is what pcbnew itself writes: the format
    has no way to say "one copper layer", and such a file reloads as 2-layer.
    """
    inner = max(count, 2) - 2
    return [
        ["0", '"F.Cu"', "signal"],
        *[[str(2 * n + 2), f'"In{n}.Cu"', "signal"] for n in range(1, inner + 1)],
        ["2", '"B.Cu"', "signal"],
    ]


def _copper_count(project: dict) -> int:
    """``project.stackup.layers``, or 2 when the HDM does not say.

    The default is what every board was emitted as before the stackup was
    read, so an HDM written without a stackup comes out as it always did.
    """
    raw = (project.get("stackup") or {}).get("layers", 2)
    # Checked before any conversion: hdm.yaml is hand-editable and Stage 6 does
    # not run it through the schema, and int(4.5) is a quiet 4-layer board.
    # bool is excluded because True == 1.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
            or raw not in _COPPER_COUNTS:
        raise ValueError(
            f"project.stackup.layers must be one of "
            f"{', '.join(map(str, sorted(_COPPER_COUNTS)))}, not {raw!r}")
    return int(raw)


def _copper_names(hdm: dict) -> set[str]:
    """The copper layer names the emitted board has, by the stackup's count.

    For a 1-layer stackup that is F.Cu alone, although the table carries B.Cu
    (see _copper_layers): a keepout on B.Cu would sit on copper the design
    says is not there.
    """
    count = _copper_count(hdm.get("project") or {})
    names = {row[1].strip('"') for row in _copper_layers(count)}
    return names - {"B.Cu"} if count == 1 else names


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
        footprint_node.remove(at_node)
    # Where KiCad writes it: straight after the footprint's layer (and uuid,
    # if that follows). Position is not cosmetic. KiCad's parser applies the
    # footprint's (at ...) when it reaches it, moving and turning whatever it
    # has already read — so an (at ...) after the pads turns them a second
    # time, and moves the footprint's board-coordinate zones by the position
    # again. Inserting "after the last layer or uuid" found the uuid
    # _replace_uuid had appended at the very end, and did exactly that.
    insert_idx = 1
    for i, child in enumerate(footprint_node[1:], start=1):
        if isinstance(child, list) and sexpr.head(child) == "layer":
            insert_idx = i + 1
            nxt = footprint_node[insert_idx] if insert_idx < len(footprint_node) else None
            if isinstance(nxt, list) and sexpr.head(nxt) == "uuid":
                insert_idx += 1
            break
    footprint_node.insert(insert_idx, new_at)


# Orienting a footprint ------------------------------------------------------
#
# A footprint on the board is not the library footprint with a different
# ``(at ...)``. KiCad's file stores a footprint's children in its own frame, but
# the *angle* on every pad and text is absolute — the footprint's orientation
# is already added in — and a footprint's zones are in board coordinates. A
# bottom-side footprint is more different still: every child is mirrored, every
# F.* layer is B.* and back, and texts are mirrored.
#
# The emitter used to do none of it. A rotated part kept its pads at their
# library angle, so KiCad loaded them turned against their own footprint (an
# 0603 at 90 deg had pads 0.8 wide across the part instead of along it). A
# bottom-side part was only relabelled ``(layer "B.Cu")``: its pads stayed on
# F.Cu/F.Mask/F.Paste with unmirrored coordinates, which is a front-side land
# pattern nobody can solder to under a back-side label.
#
# ``side: bottom, rot: r`` means what the KiCad plugin's ``hdm_to_pcb`` makes
# of it: the part as it would sit on top at ``r``, then turned over left to
# right about its origin (``FOOTPRINT::Flip`` with ``FLIP_DIRECTION::LEFT_RIGHT``).
# KiCad stores that as orientation ``180 - r`` with the children mirrored
# top-to-bottom in the footprint's frame, which is what is written here —
# measured against footprints pcbnew flipped itself, not derived, because the
# text rules in particular are not what anyone would guess.

#: Child tags whose coordinate pairs are points in the footprint's frame.
_POINT_TAGS = frozenset({"at", "start", "end", "mid", "center", "xy"})
_TEXT_TAGS = frozenset({"property", "fp_text"})
_GRAPHIC_TAGS = frozenset({"fp_line", "fp_arc", "fp_circle", "fp_rect", "fp_poly", "fp_curve"})
_JUSTIFY_SWAP = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
_CHAMFER_SWAP = {"top_left": "bottom_left", "bottom_left": "top_left",
                 "top_right": "bottom_right", "bottom_right": "top_right"}


def _flip_layer_name(name: str) -> str:
    """``F.SilkS`` -> ``B.SilkS`` and back; wildcards and inner layers unchanged."""
    if name.startswith("F."):
        return "B." + name[2:]
    if name.startswith("B."):
        return "F." + name[2:]
    return name


def _flip_layers(node: sexpr.Node) -> None:
    for tag in ("layer", "layers"):
        for layer_node in sexpr.find_all(node, tag):
            layer_node[1:] = [
                sexpr.quote(_flip_layer_name(sexpr.unquote(a))) if isinstance(a, str) else a
                for a in layer_node[1:]
            ]


def _mirror_points(node: sexpr.Node) -> None:
    """Negate y on every point under ``node``, however deeply nested.

    Deep because polygons keep their vertices in a nested ``(pts (xy ..))`` and
    a custom pad keeps its shape in ``(primitives (gr_poly (pts ..)))``; a flip
    that reads only direct children leaves exactly those behind.
    """
    for child in node:
        if not isinstance(child, list) or not child:
            continue
        if child[0] in _POINT_TAGS and len(child) >= 3:
            child[2] = _fmt(-_num(child[2]))
        elif child[0] == "offset" and len(child) >= 3:
            # A drill offset, in the pad's frame.
            child[2] = _fmt(-_num(child[2]))
        else:
            _mirror_points(child)


def _angle_of(at: sexpr.Node) -> float:
    return _num(at[3]) if len(at) >= 4 else 0.0


def _set_angle(at: sexpr.Node, angle: float) -> None:
    angle = angle % 360.0
    if abs(angle - 360.0) < 1e-9:
        angle = 0.0
    del at[3:]
    at.append(_fmt(angle))


def _justify(text: sexpr.Node) -> sexpr.Node | None:
    effects = sexpr.find(text, "effects")
    return None if effects is None else sexpr.find(effects, "justify")


def _toggle_text_mirror(text: sexpr.Node) -> None:
    effects = sexpr.find(text, "effects")
    if effects is None:
        effects = ["effects", ["font", ["size", "1", "1"], ["thickness", "0.15"]]]
        text.append(effects)
    justify = sexpr.find(effects, "justify")
    if justify is None:
        effects.append(["justify", "mirror"])
    elif "mirror" in justify:
        justify.remove("mirror")
        if len(justify) == 1:
            effects.remove(justify)
    else:
        justify.append("mirror")


def _orient_text(text: sexpr.Node, rot: float, bottom: bool) -> None:
    """A property or fp_text as KiCad leaves it after rotating and flipping.

    On the bottom the angle becomes ``-(a + rot)``, text on an F./B. or copper
    layer is mirrored, and
    then — unless the text is ``(unlocked yes)``, KiCad's "keep upright" off —
    an angle of 180 or more is brought back by 180 with both justifications
    swapped, which is the same box read the right way up. That last step is
    ``PCB_TEXT::KeepUpright``; leaving it out puts every reference on a
    flipped part upside-down.
    """
    at = sexpr.find(text, "at")
    if at is None:
        return
    a = _angle_of(at)
    if not bottom:
        _set_angle(at, a + rot)
        return
    at[2] = _fmt(-_num(at[2]))
    angle = -(a + rot) % 360.0
    layer = sexpr.find(text, "layer")
    name = sexpr.unquote(str(layer[1])) if layer is not None else ""
    if name[:2] in ("F.", "B.") or name.endswith(".Cu"):
        # Only text on a sided or copper layer is read through the board. A
        # note on User.Drawings is read from above whichever side its part is
        # on, and KiCad leaves it unmirrored.
        _toggle_text_mirror(text)
    unlocked = sexpr.find(text, "unlocked")
    keep_upright = unlocked is None or sexpr.unquote(str(unlocked[1])) != "yes"
    if keep_upright and angle >= 180.0 - 1e-9:
        angle -= 180.0
        justify = _justify(text)
        if justify is not None:
            justify[1:] = [_JUSTIFY_SWAP.get(j, j) for j in justify[1:]]
    _set_angle(at, angle)


def _flip_text_box(box: sexpr.Node) -> None:
    """An fp_text_box turned over with its footprint.

    Unlike fp_text, a text box keeps its angle relative to the footprint, so
    rotation leaves it alone and only a flip changes it: corners mirrored,
    layer swapped, text mirrored on a sided layer, and the angle ``a`` becomes
    ``180 - a``, which is what pcbnew writes. Justification is not swapped —
    a box has no keep-upright.
    """
    _mirror_points(box)
    _flip_layers(box)
    layer = sexpr.find(box, "layer")
    name = sexpr.unquote(str(layer[1])) if layer is not None else ""
    if name[:2] in ("F.", "B.") or name.endswith(".Cu"):
        _toggle_text_mirror(box)
    angle = sexpr.find(box, "angle")
    flipped = _fmt((180.0 - (_num(angle[1]) if angle is not None else 0.0)) % 360.0)
    if angle is not None:
        angle[1:] = [flipped]
    else:
        anchor = sexpr.find(box, "pts") or sexpr.find(box, "end")
        box.insert(box.index(anchor) + 1 if anchor is not None else len(box),
                   ["angle", flipped])


def _orient_pad(pad: sexpr.Node, rot: float, bottom: bool) -> None:
    at = sexpr.find(pad, "at")
    a = _angle_of(at) if at is not None else 0.0
    if bottom:
        # Mirroring the footprint mirrors the pad's own frame with it, so its
        # angle, its primitives, a trapezoid's taper and its chamfered
        # corners all turn over too.
        _mirror_points(pad)
        _flip_layers(pad)
        delta = sexpr.find(pad, "rect_delta")
        if delta is not None and len(delta) >= 3:
            delta[2] = _fmt(-_num(delta[2]))
        chamfer = sexpr.find(pad, "chamfer")
        if chamfer is not None:
            chamfer[1:] = [_CHAMFER_SWAP.get(c, c) for c in chamfer[1:]]
        a = 180.0 - rot - a
    else:
        a = a + rot
    if at is not None:
        _set_angle(at, a)


def _orient_zone(zone: sexpr.Node, x: float, y: float, orient: float, bottom: bool) -> None:
    """A footprint's zone, which KiCad stores in board coordinates."""
    if bottom:
        _mirror_points(zone)
        _flip_layers(zone)
    theta = math.radians(orient)
    c, s = math.cos(theta), math.sin(theta)

    def place(node: sexpr.Node) -> None:
        for child in node:
            if not isinstance(child, list) or not child:
                continue
            if child[0] == "xy" and len(child) >= 3:
                px, py = _num(child[1]), _num(child[2])
                child[1] = _fmt(x + px * c + py * s)
                child[2] = _fmt(y - px * s + py * c)
            else:
                place(child)

    for tag in ("polygon", "filled_polygon"):
        for poly in sexpr.find_all(zone, tag):
            place(poly)


def _orient_footprint(
    footprint_node: sexpr.Node, x: float, y: float, rot: float, side: str
) -> None:
    """Put a library footprint on the board at (x, y), turned ``rot``, on ``side``.

    ``x``/``y`` are sheet coordinates. Everything the footprint carries is
    rewritten the way KiCad would write it — see the block comment above.
    """
    bottom = side.lower() == "bottom"
    orient = (180.0 - rot if bottom else rot) % 360.0
    layer_node = sexpr.find(footprint_node, "layer")
    if layer_node is not None:
        layer_node[:] = ["layer", '"B.Cu"' if bottom else '"F.Cu"']
    for child in footprint_node:
        if not isinstance(child, list) or not child:
            continue
        tag = child[0]
        if tag in _TEXT_TAGS:
            _orient_text(child, rot, bottom)
            if bottom:
                _flip_layers(child)
        elif tag == "pad":
            _orient_pad(child, rot, bottom)
        elif tag in _GRAPHIC_TAGS:
            if bottom:
                _mirror_points(child)
                _flip_layers(child)
        elif tag == "fp_text_box" and bottom:
            _flip_text_box(child)
        elif tag == "zone":
            _orient_zone(child, x, y, orient, bottom)
    _override_footprint_position(footprint_node, x, y, orient)


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


# The courtyard layers, and only those. A zero-length segment is meaningless on
# any layer, but it is only *consequential* on a courtyard — see below.
_COURTYARD_LAYERS = frozenset({"F.CrtYd", "B.CrtYd"})


def _set_assembly(fp_node: sexpr.Node, assembly: str | None) -> None:
    """Mark a footprint that is not bought and placed (project.yaml assembly).

    Both kinds stay on the board and leave the BOM and the placement file;
    a DNP part is also marked DNP, so the board says the part exists but is
    not fitted. The footprint's own attr (smd, through_hole) is kept.
    """
    attr = sexpr.find(fp_node, "attr")
    if assembly != "dnp" and attr is not None and "dnp" in attr:
        # Whether a part is fitted is the design's to say, not its library's:
        # Seeed's Wio-LR2021 footprint ships marked DNP, which dropped the
        # radio module from the placement file and the BOM of every board
        # that used it. Only assembly.dnp marks a part DNP.
        attr.remove("dnp")
    if assembly not in ("dnp", "footprint_only"):
        return
    flags = ["exclude_from_pos_files", "exclude_from_bom"] + (["dnp"] if assembly == "dnp" else [])
    if attr is None:
        fp_node.append(["attr", *flags])
        return
    for flag in flags:
        if flag not in attr[1:]:
            attr.append(flag)


def _drop_degenerate_lines(fp_node: sexpr.Node) -> int:
    """Remove zero-length ``fp_line`` segments from a footprint's courtyard.

    A zero-length line has no geometry, so dropping one cannot change a shape.
    What it changes is whether KiCad can *close* the courtyard, and that is
    worth a great deal: the Specctra export emits a component's keep-out as a
    closed ``(outline (polygon ...))``, and a courtyard it cannot close yields
    no polygon at all. The autorouter then has nothing to route around and puts
    tracks through the part.

    Measured on core, exporting the same board before and after this runs.
    ``CSD25501F3`` has a 1.2446 x 1.1430 mm rectangular courtyard drawn with
    twelve segments, four of them zero-length. Before: six outlines, none of
    them a polygon — only open path fragments. After: seven, the new one being
    ``(outline (polygon signal 0 617.3 -566.5 -617.3 -566.5 -617.3 566.5 617.3
    566.5))``, which is that rectangle. Board-wide the DSN goes from 429
    outlines to 430: exactly one part gained a keep-out it never had.

    ``MM8108`` on sb-halow was the same defect at a larger scale and is where
    it first showed: twenty courtyard segments, eight of them zero-length, and
    an export carrying fifty-one outlines for it of which *zero* were polygons.
    That is how sb-halow once reached "219 segments, 0 unrouted" — by routing
    under the largest part on the board. It was repaired in the library; this
    catches the rest.

    Restricted to the courtyard layers because that is where the harm is, and
    because touching the others costs something. Across stock kicad-footprints
    plus this project's libraries, 143 footprints carry a zero-length segment
    somewhere, but 137 of those are on silkscreen or fab, where nothing is
    built from the geometry and the segment is simply invisible. Stripping
    those would rewrite 137 footprints to no effect and earn each one a
    ``lib_footprint_mismatch``, because the board copy would stop matching the
    library copy. ``ICM42670P`` is the worked example: two zero-length
    segments, both ``F.SilkS``, whose removal bought nothing and cost a
    warning. Six footprints have it on ``F.CrtYd``; those are the ones here.

    The check that cannot be fooled, and the one to reach for when changing
    any of this, is to ask pcbnew directly rather than to count s-expressions::

        fp.GetCourtyard(pcbnew.F_CrtYd).OutlineCount()   # 0 means no keep-out

    On core before this ran, exactly one of 161 footprints answered 0 —
    ``Q_BAT_REV``. After, none do. A grep cannot see that and neither can
    ``kicad-cli fp export svg``, which happily plots a footprint whose
    courtyard will not close.

    Done in the emitter rather than only in libraries because most of the six
    live in stock kicad-footprints — SOT-723, Telit_xL865 and nRF24L01_Breakout
    among them — which is an upstream submodule this project does not own.
    Cleaning on the way into the board fixes every footprint, whoever wrote it,
    and keeps it fixed across the next upstream bump.
    """
    dropped = 0
    for line in list(sexpr.find_all(fp_node, "fp_line")):
        layer = sexpr.find(line, "layer")
        if layer is None or len(layer) < 2:
            continue
        if str(layer[1]).strip('"') not in _COURTYARD_LAYERS:
            continue
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


def _mm(v: float) -> str:
    """A coordinate as KiCad wants it: plain decimal, no exponent, no float noise."""
    r = round(float(v), 6)
    out = f"{r:.6f}".rstrip("0").rstrip(".")
    return "0" if out in ("", "-0") else out


def _sheet(p) -> tuple[str, str]:
    return _mm(float(p[0]) + _ORIGIN_X), _mm(float(p[1]) + _ORIGIN_Y)


def _edge_cuts_shape(segments: list[dict]) -> list[sexpr.Node]:
    """Edge.Cuts graphics for one closed path from Stage 5's ``board_geometry``.

    Arcs stay arcs and a circle stays a circle. Flattening them here would
    give the fab a faceted edge it was never asked for, and give DRC a
    polygon that is not the board the outline describes.
    """
    stroke = ["stroke", ["width", f"{_EDGE_CUTS_WIDTH}"], ["type", "solid"]]
    out: list[sexpr.Node] = []
    for seg in segments:
        kind, pts = seg["kind"], seg["points"]
        if kind == "line":
            node = ["gr_line", ["start", *_sheet(pts[0])], ["end", *_sheet(pts[1])], stroke]
        elif kind == "arc":
            node = ["gr_arc", ["start", *_sheet(pts[0])], ["mid", *_sheet(pts[1])],
                    ["end", *_sheet(pts[2])], stroke]
        elif kind == "circle":
            c = pts[0]
            node = ["gr_circle", ["center", *_sheet(c)],
                    ["end", *_sheet((float(c[0]) + float(seg["radius"]), c[1]))],
                    stroke, ["fill", "no"]]
        else:
            raise ValueError(f"board_geometry segment of unknown kind {kind!r}")
        out.append([*node, ["layer", '"Edge.Cuts"'], ["uuid", _new_uuid()]])
    return out


def _keepout_zone(feature: dict, copper: set[str]) -> sexpr.Node:
    """A board-level rule area, laid out like the ones stock RF module footprints carry.

    No net: a rule area has none, and leaving the field out is what the
    footprint libraries do, so the file reads the same under KiCad 9 and 10
    whichever net syntax each prefers.

    ``copper`` is the board's copper layer names. Any name that is neither
    one of those nor a layer in the table — a copper layer the board does not
    have, or a misspelling — is a zone on nothing: KiCad loads it and it
    protects nothing. Stage 5 refuses those with the reason; this is the
    backstop for an HDM that did not come through it.
    """
    layers = feature.get("layers") or ["*.Cu"]
    outer = {"F&B.Cu"} if "B.Cu" in copper else set()
    non_copper = {row[1].strip('"') for row in _NON_COPPER_LAYERS}
    missing = [layer for layer in layers
               if layer not in copper | outer | non_copper | {"*.Cu"}]
    if missing:
        raise ValueError(
            f"keepout {feature.get('where') or feature.get('name') or ''} is on "
            f"{', '.join(missing)}, which this board's stackup does not have "
            f"(copper: {', '.join(sorted(copper))})")
    forbid = set(feature.get("forbid") or ())
    rules = [[k, "not_allowed" if k in forbid else "allowed"]
             for k in ("tracks", "vias", "pads", "copperpour", "footprints")]
    node: sexpr.Node = [
        "zone",
        ["layers", *[sexpr.quote(layer) for layer in layers]],
        ["uuid", _new_uuid()],
    ]
    if feature.get("name"):
        node.append(["name", sexpr.quote(str(feature["name"]))])
    node += [
        ["hatch", "edge", "0.5"],
        ["connect_pads", ["clearance", "0"]],
        ["min_thickness", "0.25"],
        ["filled_areas_thickness", "no"],
        ["keepout", *rules],
        ["placement", ["enabled", "no"], ["sheetname", '""']],
        ["fill", ["thermal_gap", "0.5"], ["thermal_bridge_width", "0.5"]],
        ["polygon", ["pts", *[["xy", *_sheet(p)] for p in feature["polygon"]]]],
    ]
    return node


def _plane_zone(plane: dict, ordinals: dict[str, int], copper: set[str]) -> sexpr.Node:
    """A copper zone filling ``plane["layer"]`` with ``plane["net"]``.

    Unfilled as written: KiCad fills it on load in the editor, Stage 7's DRC
    refills before checking, and the autoroute refills after importing the
    route. Pads join it with thermal reliefs, the default a hand-drawn plane
    gets in the editor.
    """
    layer, net = plane["layer"], plane["net"]
    if layer not in copper:
        raise ValueError(f"plane for {net} is on {layer}, which this board does not have "
                         f"(copper: {', '.join(sorted(copper))})")
    if net not in ordinals:
        raise ValueError(f"plane for {net}: no such net on this board")
    return [
        "zone",
        ["net", str(ordinals[net])],
        ["net_name", sexpr.quote(net)],
        ["layer", sexpr.quote(layer)],
        ["uuid", _new_uuid()],
        ["name", sexpr.quote(f"{net} plane")],
        ["hatch", "edge", "0.5"],
        ["connect_pads", ["clearance", "0.3"]],
        ["min_thickness", "0.25"],
        ["filled_areas_thickness", "no"],
        ["fill", "yes", ["thermal_gap", "0.5"], ["thermal_bridge_width", "0.5"]],
        ["polygon", ["pts", *[["xy", *_sheet(p)] for p in plane["polygon"]]]],
    ]


def _board_shapes(hdm: dict, width: float, height: float) -> list[sexpr.Node]:
    """Outline, cutouts and keepouts — from Stage 5's resolved geometry when present.

    An HDM without ``board_geometry`` predates shaped outlines (or was written
    by hand); it gets the rectangle its dimensions describe, as before.
    """
    geom = hdm.get("board_geometry")
    if not isinstance(geom, dict):
        return _edge_cuts_rect(width, height)
    out = _edge_cuts_shape(geom["outline"]["segments"])
    for feature in geom.get("features") or []:
        if feature.get("kind") == "cutout":
            out += _edge_cuts_shape(feature["segments"])
        elif feature.get("kind") == "keepout" and not feature.get("anchor"):
            # Anchored keepouts go inside their footprint; see _anchored_keepouts.
            out.append(_keepout_zone(feature, _copper_names(hdm)))
    return out


def _anchored_keepouts(hdm: dict, refdes: str) -> list[sexpr.Node]:
    """Keepouts anchored to ``refdes``, as zones owned by its footprint.

    Owned by the footprint, not the board, because KiCad exempts a rule area's
    own footprint from it and nothing else. An antenna keepout drawn at board
    level over its module reported the module's own pads and footprint as
    "items not allowed" — the keepout doing its job against the one part it
    exists for. Inside the footprint it behaves like the keepout a stock RF
    module footprint ships with.
    """
    geom = hdm.get("board_geometry")
    if not isinstance(geom, dict):
        return []
    copper = _copper_names(hdm)
    return [_keepout_zone(f, copper) for f in geom.get("features") or []
            if f.get("kind") == "keepout" and f.get("anchor") == refdes]


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
    # A layer a plane fills is a power layer, not a signal one: so marked,
    # KiCad's Specctra export tells Freerouting it is a plane, and the router
    # reaches it with a via instead of routing other nets across it. Left as
    # signal, the planes reached Freerouting and changed nothing: 28 open
    # connections against 26 without them on verdacor-ear-tag.
    plane_layers = {p["layer"] for p in ((hdm.get("board_geometry") or {}).get("planes") or [])}
    copper_rows = [[n, name, "power" if name.strip('"') in plane_layers else kind]
                   for n, name, kind in _copper_layers(_copper_count(project))]
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
        ["layers", *copper_rows, *_NON_COPPER_LAYERS],
        _default_setup(),
    ]

    # Nets.
    for ordinal, name in net_table:
        node.append(["net", str(ordinal), sexpr.quote(name)])

    # Board outline and cutouts on Edge.Cuts; keepouts as rule areas.
    for shape in _board_shapes(hdm, width, height):
        node.append(shape)
    # Planes: a copper zone per plane, on its net (see core/planes.py).
    ordinals = {name: ordinal for ordinal, name in net_table}
    for plane in ((hdm.get("board_geometry") or {}).get("planes") or []):
        node.append(_plane_zone(plane, ordinals, _copper_names(hdm)))

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

        _set_reference_and_value(fp_node, refdes, str(comp.get("value", refdes)))
        _replace_uuid(fp_node)
        _set_assembly(fp_node, comp.get("assembly"))

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

        # Last, so that whatever was added above — the placeholder marker
        # among it — turns over with the rest of the part.
        _orient_footprint(fp_node, sheet_x, sheet_y, rot, side)
        # After orienting, not before: Stage 5 has already placed these in
        # board coordinates, mirrored with a bottom-side part and with their
        # layers swapped (board_geometry.place_anchored), so turning them
        # again would put them back on the wrong side.
        fp_node.extend(_anchored_keepouts(hdm, refdes))
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
