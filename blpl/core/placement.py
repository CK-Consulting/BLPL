"""Put components on the board, inside its outline, without overlapping.

What was here before was a 15 mm grid: every component the same size, laid out
in rows that simply kept going past the bottom edge. On example-handheld
that put **94 of core's 130 parts outside the board**, and sb-ant's column
reached y=455 mm on a 35 mm board. It was never meant as placement — the
docstring called it "so they're visible in KiCad" — but it is what the router
was being handed, and a router cannot do anything useful with parts that are
not on the board.

Three things this does that the grid could not.

**Real sizes.** A component's extent comes from its footprint's courtyard,
which is the outline the part is *entitled* to and already carries the
clearance the library author intended. Failing that, the pad bounding box plus
a margin; failing that, a conservative default. Sizes are what make
non-overlap meaningful: on a fixed grid a 13 mm BGA and an 0402 occupy the same
cell, so the grid is either wasteful or wrong, and at 15 mm it was both.

**Locality, from the nets that carry it.** Components are placed in order of
how strongly they are tied to what is already down, each one as close as it can
get to the centroid of its placed neighbours. Power and ground are excluded
from that adjacency deliberately: GND touches almost every part on the board,
so counting it makes every part equally adjacent to every other and the signal
that remains is noise.

**And the locality nets cannot carry.** Excluding the rails has a cost that is
easy to miss and expensive to ship: a decoupling capacitor sits on a rail and
ground and *nothing else*, so once the rails are excluded it has no neighbours
at all and lands wherever there is room. Measured on core, the first version of
this module put `C_U1_VDD1` **42 mm from U1** — the one placement constraint on
a digital board that is genuinely non-negotiable, failed completely, while the
wirelength score improved 73% because that score ignores the rails too. A
metric cannot see what it excludes.

Nets genuinely cannot answer it either: every IC on the board shares that rail,
so no amount of netlist analysis says *which* IC a bypass cap belongs to. What
does say so is the name the designer gave it. `C_U1_VDD1` names U1, `C_ALS`
names U_ALS — an extremely common convention, and where it holds this module
follows it. Where it does not, ``hints`` takes a ``near``, and that is the
honest division: a convention is read, never assumed.

**Refusal.** The grid's real failure was silence — it always "succeeded", and
the overflow only showed up much later as a board that could not be routed.
This returns what it could not place and why, and the caller decides. A placer
that quietly puts a part at (5, 455) has not placed it.

What this is not: a floorplanner. It does not know that an RF trace wants to be
short, that a switching node wants to be tiny, or that a crystal wants to be
next to its oscillator. Those are recorded per board in the design markdown —
``L_DCC`` hard against U_BLE pins 28 and 30, the FOD resistors tight to U_WPC
with a short PGND return — and belong in ``hints``, which a human writes and
this module honours rather than guesses.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from blpl.emitter import loaders, sexpr

#: Edge-to-edge gap between neighbouring courtyards. The courtyard already
#: carries the part's own clearance, so this is room to route between parts
#: rather than a manufacturing minimum.
DEFAULT_CLEARANCE = 0.5

#: Distance from the board edge that nothing but edge-seeking parts may enter.
#: Board houses need a margin for routing and V-scoring, and a connector that
#: overhangs is a connector that does not fit its enclosure.
DEFAULT_EDGE_MARGIN = 1.0

#: When a footprint yields no geometry at all. Deliberately large: a part whose
#: size is unknown should crowd its neighbours rather than silently overlap
#: them, and an oversized keep-out is visible in the editor where a missing one
#: is not.
FALLBACK_EXTENT = (5.0, 5.0)

#: A part at least this big is placed before the passives, largest first.
#: Modules, connectors and large ICs need contiguous space, and space is
#: contiguous only while it is still empty: sb-lora's LR2021 module
#: (17.1 x 11.0 mm) could not be placed at all on a board that was 44% full,
#: because a few dozen 0402s had already been scattered by connectivity alone
#: and nothing contiguous was left. Humans floorplan the big parts first for
#: exactly this reason.
MAJOR_AREA_MM2 = 20.0

#: Nets excluded from the adjacency that drives clustering. A net on nearly
#: every part says nothing about which parts belong together.
_GLOBAL_NET_HINTS = ("GND", "AGND", "DGND", "PGND", "VSS", "VDD", "VCC")

#: Refdes prefixes that want the board edge: something outside the board plugs
#: into them, so an interior position is wrong however well it routes.
_EDGE_PREFIXES = ("J", "SW", "TP", "BZ", "SPKR", "M_", "ANT")


@dataclass
class Placement:
    x: float
    y: float
    rot: float = 0.0
    side: str = "top"

    def as_dict(self) -> dict:
        return {"x": round(self.x, 3), "y": round(self.y, 3),
                "rot": float(self.rot), "side": self.side}


@dataclass
class Result:
    placements: dict[str, Placement] = field(default_factory=dict)
    #: refdes -> why it could not be placed. Non-empty means the board is too
    #: small, or the parts on it are too big, and that is a design answer.
    unplaced: dict[str, str] = field(default_factory=dict)
    #: refdes -> (w, h) actually used, so a caller can explain a refusal.
    extents: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: refdes -> where the collision box sits relative to the footprint origin.
    offsets: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: Refdes whose extent is FALLBACK_EXTENT because the footprint yielded no
    #: geometry. Kept separate because a guessed size is not evidence of a
    #: large part, and the big-parts-first pass must not act on one.
    assumed: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unplaced


def footprint_box(
    ref: str, roots: Sequence[Path | str]
) -> tuple[float, float, float, float] | None:
    """(width, height, dx, dy) in mm, preferring the courtyard.

    ``dx``/``dy`` locate the box's centre **relative to the footprint origin**,
    and ignoring them is a real error rather than a rounding one: a footprint's
    origin is wherever its author put it, which for most connectors is pin 1 at
    one end. Assuming the courtyard is centred on the origin puts the collision
    box beside the part rather than on it, so two footprints can be declared
    clear while their courtyards overlap — which is exactly what KiCad's DRC
    found on sb-lora, four times, on a board this module had just certified as
    overlap-free.

    The courtyard is the right source: it is the area the part is entitled to,
    already including whatever clearance the library author judged necessary.
    Pads alone understate a part whose body overhangs them, which is most
    connectors.
    """
    try:
        fp = loaders.load_footprint(ref, roots)
    except Exception:
        return None

    courtyard: list[tuple[float, float]] = []
    everything: list[tuple[float, float]] = []

    def points(node) -> list[tuple[float, float]]:
        """Every coordinate under a shape, including nested ``(pts ...)``.

        Polygons keep their vertices one level down, in a ``pts`` child. Reading
        only direct children therefore skipped polygon courtyards **entirely and
        silently**, falling back to whatever pads happened to be there: the U.FL
        jack measured 3.85 x 3.50 mm against a real courtyard of 5.10 x 5.00,
        so three of them were placed 4 mm apart and KiCad found all three pairs
        overlapping.

        That is the third time geometry that was not read produced a box too
        small to collide with — a circle's radius, a degenerate axis, and now a
        polygon. Under-measuring never announces itself: the placement simply
        succeeds, and the overlap surfaces in DRC long afterwards.
        """
        out = []
        for child in node:
            if not isinstance(child, list) or not child:
                continue
            if child[0] in ("start", "end", "center", "mid", "at", "xy"):
                try:
                    out.append((float(child[1]), float(child[2])))
                except (IndexError, ValueError):
                    pass
            elif child[0] == "pts":
                out.extend(points(child))
        return out

    def layer_of(node) -> str:
        for child in node:
            if isinstance(child, list) and len(child) >= 2 and child[0] == "layer":
                return sexpr.unquote(str(child[1]))
        return ""

    def walk(node) -> None:
        if not isinstance(node, list):
            return
        tag = node[0] if node and isinstance(node[0], str) else ""
        if tag in ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc", "pad"):
            pts = points(node)
            if tag == "fp_circle" and len(pts) >= 2:
                # A circle gives a centre and a point on the circumference, not
                # two opposite corners. Treating them as corners collapses the
                # box to a line: a round test-point courtyard came out 1.0 x 0.0
                # mm, so the part had no height, overlapped its neighbour, and
                # KiCad's DRC found the courtyards crossing on a board this
                # module had just certified as overlap-free.
                (cx, cy), (ex, ey) = pts[0], pts[1]
                r = math.hypot(ex - cx, ey - cy)
                pts = [(cx - r, cy - r), (cx + r, cy + r)]
            everything.extend(pts)
            if layer_of(node).endswith("CrtYd"):
                courtyard.extend(pts)
        for child in node:
            walk(child)

    walk(fp)
    pts = courtyard or everything
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    dx = (max(xs) + min(xs)) / 2
    dy = (max(ys) + min(ys)) / 2
    if not courtyard:
        # Pads only: add a little, because the body is wider than the copper.
        w, h = w + DEFAULT_CLEARANCE, h + DEFAULT_CLEARANCE
    if w <= 0 or h <= 0:
        # A degenerate box is not a small part, it is a part this function
        # failed to measure — and a zero dimension means every overlap test
        # against it passes. Refuse the answer rather than return one that
        # quietly disables the collision check.
        return None
    return (w, h, dx, dy)


def footprint_extent(
    ref: str, roots: Sequence[Path | str]
) -> tuple[float, float] | None:
    """(width, height) only, for callers that do not place anything."""
    box = footprint_box(ref, roots)
    return None if box is None else (box[0], box[1])


def _is_global(net_name: str) -> bool:
    upper = net_name.upper()
    return any(upper == h or upper.startswith(h + "_") or upper.startswith(h) and upper[:3] in ("GND", "VDD", "VCC", "VSS")
               for h in _GLOBAL_NET_HINTS)


def _adjacency(nets: dict, refs: set[str]) -> dict[str, dict[str, int]]:
    """refdes -> {neighbour: shared local nets}, global rails excluded."""
    adj: dict[str, dict[str, int]] = {r: {} for r in refs}
    for name, net in (nets or {}).items():
        if _is_global(str(name)):
            continue
        members = sorted({str(p[0]) for p in (net.get("pads") or []) if p and str(p[0]) in refs})
        # A net on half the board is a bus, not a hint about neighbours.
        if len(members) < 2 or len(members) > 8:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                adj[a][b] = adj[a].get(b, 0) + 1
                adj[b][a] = adj[b].get(a, 0) + 1
    return adj


def _wants_edge(ref: str) -> bool:
    return any(ref == p or ref.startswith(p) for p in _EDGE_PREFIXES)


def _anchor_by_name(ref: str, refs: set[str]) -> str | None:
    """The component this one is named after, if any.

    ``C_U1_VDD1`` -> ``U1``. Splitting on the separators refdes conventions
    actually use, then taking the longest other refdes that appears as a whole
    token — longest because ``U_CHG`` must beat a bare ``U`` and a substring
    match would find one in almost anything.

    Only consulted for parts the netlist cannot place: see the module docstring
    for why a bypass capacitor is invisible to net adjacency.
    """
    tokens = [t for t in re.split(r"[_\-.]", ref) if t]
    best: str | None = None
    for i in range(len(tokens)):
        for j in range(i + 1, len(tokens) + 1):
            candidate = "_".join(tokens[i:j])
            if candidate != ref and candidate in refs:
                if best is None or len(candidate) > len(best):
                    best = candidate
    return best


def _declared_anchor(ref: str, hints: dict, refs: set[str]) -> str | None:
    """What this part should sit beside: the hint if there is one, else the name.

    One function so that *ordering* and *positioning* cannot disagree. They did:
    a part was queued to follow the component it was named after, then placed
    against a different one its ``near`` hint gave — except the hinted anchor
    had not been placed yet, so the hint was silently ignored and the name won.
    """
    near = (hints.get(ref) or {}).get("near")
    if near:
        return str(near)
    return _anchor_by_name(ref, refs)


class _Canvas:
    """Occupied rectangles, and where the next one can go."""

    def __init__(self, w: float, h: float, clearance: float, margin: float, step: float,
                 edge_margin: float = 0.3):
        self.w, self.h = w, h
        self.clearance = clearance
        self.margin = margin
        self.edge_margin = edge_margin
        self.step = step
        self.taken: list[tuple[float, float, float, float]] = []

    def _fits(self, x: float, y: float, w: float, h: float, edge: bool) -> bool:
        # An edge-seeking part may approach the boundary, but not touch it:
        # copper hard against the board edge fails copper_edge_clearance, and a
        # routed board that cannot be milled is not a routed board.
        m = self.edge_margin if edge else self.margin
        if x - w / 2 < m or y - h / 2 < m:
            return False
        if x + w / 2 > self.w - m or y + h / 2 > self.h - m:
            return False
        c = self.clearance
        for (ox, oy, ow, oh) in self.taken:
            if abs(x - ox) < (w + ow) / 2 + c and abs(y - oy) < (h + oh) / 2 + c:
                return False
        return True

    def candidates(self, edge: bool) -> list[tuple[float, float]]:
        """Every legal centre on the search grid, cheapest first is the caller's job."""
        out = []
        n_x = max(1, int(self.w / self.step))
        n_y = max(1, int(self.h / self.step))
        for i in range(n_x + 1):
            for j in range(n_y + 1):
                out.append((i * self.step, j * self.step))
        if edge:
            # Perimeter first, so an edge-seeking part takes the edge even when
            # the interior is emptier and therefore nearer its neighbours.
            out.sort(key=lambda p: min(p[0], p[1], self.w - p[0], self.h - p[1]))
        return out

    def take(self, x: float, y: float, w: float, h: float) -> None:
        self.taken.append((x, y, w, h))


def place(
    components: dict,
    nets: dict,
    board: tuple[float, float],
    *,
    footprint_roots: Sequence[Path | str] = (),
    hints: dict | None = None,
    clearance: float = DEFAULT_CLEARANCE,
    edge_margin: float = DEFAULT_EDGE_MARGIN,
) -> Result:
    """Place every component, or say which ones would not fit.

    ``hints`` maps a refdes to a partial placement — ``{"x":…, "y":…}``,
    ``{"rot":…}``, ``{"side":"bottom"}``, or ``{"near": "U1"}``. A hint is an
    instruction, not a preference: a hinted position is taken as given and
    reserved before anything else is placed, because a human writing one knows
    something this module does not.
    """
    hints = hints or {}
    result = Result()
    bw, bh = board
    refs = set(components)

    offsets: dict[str, tuple[float, float]] = {}
    for ref, comp in components.items():
        fp = comp.get("footprint")
        box = footprint_box(fp, footprint_roots) if fp else None
        if box is None:
            ext = FALLBACK_EXTENT
            offsets[ref] = (0.0, 0.0)
            result.assumed.add(ref)
            result.notes.append(f"{ref}: no footprint geometry; assumed {ext[0]}x{ext[1]}mm")
        else:
            ext = (box[0], box[1])
            offsets[ref] = (box[2], box[3])
        result.extents[ref] = ext
    result.offsets = offsets

    step = max(0.5, min(min(e) for e in result.extents.values()) / 2) if result.extents else 1.0
    canvas = _Canvas(bw, bh, clearance, edge_margin, step)

    # 1. Fixed hints first: they are constraints, and everything else works
    #    around them rather than the other way about.
    for ref in sorted(refs):
        hint = hints.get(ref) or {}
        if "x" in hint and "y" in hint:
            w, h = result.extents[ref]
            dx, dy = offsets[ref]
            p = Placement(float(hint["x"]), float(hint["y"]),
                          float(hint.get("rot", 0.0)), str(hint.get("side", "top")))
            result.placements[ref] = p
            # A hint gives the footprint origin; the box it occupies is offset
            # from that by wherever its author put the origin.
            canvas.take(p.x + dx, p.y + dy, w, h)

    adj = _adjacency(nets, refs)
    remaining = [r for r in sorted(refs) if r not in result.placements]

    # 2. The big parts, largest first, before anything scatters the space they
    #    need. Position still comes from connectivity — this decides *order*,
    #    which is the whole difference between a module that fits and one that
    #    reports it could not.
    majors = [r for r in remaining
              if r not in result.assumed and _area(result.extents[r]) >= MAJOR_AREA_MM2]
    majors.sort(key=lambda r: (-_area(result.extents[r]), r))
    for ref in majors:
        w, h = result.extents[ref]
        edge = _wants_edge(ref)
        anchors = [result.placements[n] for n in adj[ref] if n in result.placements]
        if anchors:
            tx = sum(a.x for a in anchors) / len(anchors)
            ty = sum(a.y for a in anchors) / len(anchors)
        else:
            tx, ty = bw / 2, bh / 2
        spot = next(
            ((x, y) for (x, y) in _ranked_spots(canvas, edge, tx, ty)
             if canvas._fits(x, y, w, h, edge)),
            None,
        )
        if spot is None:
            result.unplaced[ref] = (
                f"no free {w:.2f}x{h:.2f}mm position inside {bw:.1f}x{bh:.1f}mm"
            )
            remaining.remove(ref)
            continue
        hint = hints.get(ref) or {}
        dx, dy = offsets[ref]
        # The search works in box centres; a placement is a footprint origin.
        result.placements[ref] = Placement(spot[0] - dx, spot[1] - dy,
                                           float(hint.get("rot", 0.0)),
                                           str(hint.get("side", "top")))
        canvas.take(spot[0], spot[1], w, h)
        remaining.remove(ref)

    # 3. Seed with the most-connected part, which is almost always the thing
    #    everything else is arranged around.
    if remaining and not result.placements:
        seed = max(remaining, key=lambda r: (sum(adj[r].values()), -_area(result.extents[r]), r))
        w, h = result.extents[seed]
        hint = hints.get(seed) or {}
        for (x, y) in sorted(canvas.candidates(False), key=lambda p: (p[0] - bw / 2) ** 2 + (p[1] - bh / 2) ** 2):
            if canvas._fits(x, y, w, h, False):
                sdx, sdy = offsets[seed]
                result.placements[seed] = Placement(
                    x - sdx, y - sdy,
                    float(hint.get("rot", 0.0)), str(hint.get("side", "top"))
                )
                canvas.take(x, y, w, h)
                remaining.remove(seed)
                break

    # 4. Then, repeatedly, whichever unplaced part is most strongly tied to
    #    what is already down — so clusters grow outward from their anchor
    #    instead of being scattered by refdes order.
    while remaining:
        def tie(r: str) -> tuple:
            placed = [(n, c) for n, c in adj[r].items() if n in result.placements]
            return (sum(c for _, c in placed), len(placed), -_area(result.extents[r]))

        # A part named after one already placed goes next, ahead of everything
        # else. Ranking it merely "above parts with no connections" was not
        # enough and the measurement said so: the IC's signal neighbours took
        # the ring around it first, and core's bypass capacitors still landed a
        # mean of 37 mm from U1 against 39 mm for no anchoring at all. A bypass
        # capacitor's constraint is tighter than any signal net's — it is the
        # reason the part exists — so it gets first claim on the space beside
        # its IC, and the signal nets arrange themselves around that.
        followers = [r for r in remaining
                     if not any(n in result.placements for n in adj[r])
                     and (_declared_anchor(r, hints, refs) or "") in result.placements]
        if followers:
            followers.sort(key=lambda r: (-_area(result.extents[r]), r))
            ref = followers[0]
            remaining.remove(ref)
        else:
            remaining.sort(key=lambda r: (tie(r), r), reverse=True)
            ref = remaining.pop(0)
        w, h = result.extents[ref]
        edge = _wants_edge(ref)

        anchors = [result.placements[n] for n in adj[ref] if n in result.placements]
        declared = _declared_anchor(ref, hints, refs)
        if declared and declared in result.placements:
            # A hint outranks the netlist; a name only speaks when the netlist
            # is silent, which for a bypass capacitor is always.
            if (hints.get(ref) or {}).get("near"):
                anchors = [result.placements[declared]]
            elif not anchors:
                anchors = [result.placements[declared]]
                result.notes.append(f"{ref}: anchored to {declared} by name")
        if anchors:
            tx = sum(a.x for a in anchors) / len(anchors)
            ty = sum(a.y for a in anchors) / len(anchors)
        else:
            tx, ty = bw / 2, bh / 2

        spot = None
        for (x, y) in _ranked_spots(canvas, edge, tx, ty):
            if canvas._fits(x, y, w, h, edge):
                spot = (x, y)
                break
        if spot is None:
            result.unplaced[ref] = (
                f"no free {w:.2f}x{h:.2f}mm position inside {bw:.1f}x{bh:.1f}mm"
            )
            continue
        hint = hints.get(ref) or {}
        dx, dy = offsets[ref]
        result.placements[ref] = Placement(spot[0] - dx, spot[1] - dy,
                                           float(hint.get("rot", 0.0)),
                                           str(hint.get("side", "top")))
        canvas.take(spot[0], spot[1], w, h)

    return result


def _ranked_spots(canvas: "_Canvas", edge: bool, tx: float, ty: float):
    """Candidate centres, best first, for a part that does or does not seek an edge.

    Both passes used to sort the perimeter-ordered candidate list by distance
    to the target, which discarded the perimeter ordering entirely — so
    ``_wants_edge`` only ever widened the margin and never moved anything. A
    30-way FPC connector landed on sb-lora's exact centre, spanning the full
    width and cutting the board in two: the LR2021 module then had nowhere to
    go, and the flex had nowhere to exit. Edge-seeking is a hard preference
    ranked *before* locality, because something outside the board plugs into
    these parts and no amount of short routing makes an interior position work.
    """
    def key(q: tuple[float, float]) -> tuple:
        near_target = math.hypot(q[0] - tx, q[1] - ty)
        if not edge:
            return (0.0, near_target)
        to_edge = min(q[0], q[1], canvas.w - q[0], canvas.h - q[1])
        return (round(to_edge, 3), near_target)

    return sorted(canvas.candidates(edge), key=key)


def _area(extent: tuple[float, float]) -> float:
    return extent[0] * extent[1]


def occupied_box(result: "Result", ref: str) -> tuple[float, float, float, float]:
    """(x_min, y_min, x_max, y_max) of what ``ref`` actually occupies.

    The placement is a footprint origin and the collision box is offset from
    it, so the two are not the same rectangle and checking the wrong one is how
    an overlap-free board acquires overlapping courtyards.
    """
    p = result.placements[ref]
    w, h = result.extents[ref]
    dx, dy = result.offsets.get(ref, (0.0, 0.0))
    cx, cy = p.x + dx, p.y + dy
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def total_connection_length(placements: dict[str, Placement], nets: dict) -> float:
    """Sum of each net's half-perimeter bounding box — the usual placement score.

    Not a routing estimate. It is a comparable number: lower means connected
    parts ended up nearer each other, which is the only thing a placer can
    actually promise the router.
    """
    total = 0.0
    for net in (nets or {}).values():
        pts = [placements[str(p[0])] for p in (net.get("pads") or [])
               if p and str(p[0]) in placements]
        if len(pts) < 2:
            continue
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total
