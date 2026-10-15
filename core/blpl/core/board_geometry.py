"""Board outline, cutouts and keepouts: one shape vocabulary for all three.

Until this module existed the board was a rectangle built from
``project.dimensions`` and nothing else. ``boundaries.board_outline`` was
written by ``blpl init``, copied into the HDM by Stage 5, accepted by the
schema with a ``polygon`` option — and read by nobody. Editing it changed
nothing and nothing said so, which is the worst kind of setting: one that
reports success. ``keepouts`` were the same, and Stage 8's own rule text
admitted the placer never looked at them.

The vocabulary, in board-local millimetres with the origin at the top-left
and y pointing down (KiCad's convention, and the HDM's):

    rect       size: [w, h], plus origin: [x, y] (top-left) or center: [x, y];
               optional corner_radius
    circle     diameter (or radius), center
    polygon    points: [[x, y], ...]; optional corner_radius
    trapezoid  bottom, top, height; optional top_offset (left end of the top
               edge relative to the bottom's, default centred)

A cutout or keepout may carry ``anchor: <refdes>``. Its coordinates are then in
that footprint's frame and it follows the part through placement — the slot or
the copper-free area under a module's PCB antenna is the case that motivated
it. Anchored shapes are resolved in Stage 5, once the part has a position.

Arcs are kept as arcs for emission (``gr_arc``/``gr_circle``) and flattened to
polygons only for the placer and the checks, which need containment tests
rather than exact curves. Chords stay within ARC_TOLERANCE_MM of the curve —
inside it on a convex arc, outside it on a rounded concave corner — and that is
an order of magnitude below the placer's edge margin, so the approximation
never decides whether a part fits.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

Point = tuple[float, float]

#: Largest gap between a flattened arc and the true curve. Small next to any
#: edge margin the placer uses, so the approximation never decides a fit.
ARC_TOLERANCE_MM = 0.02

#: Two lengths closer than this are the same length. Coordinates in a
#: hand-written YAML file are decimal millimetres; anything finer is noise.
EPS = 1e-6

#: What a keepout forbids when it does not say. Everything, because an antenna
#: keepout that quietly allows a copper pour under it is no keepout at all.
DEFAULT_FORBID = ("tracks", "vias", "pads", "copperpour", "footprints")
FORBIDDABLE = frozenset(DEFAULT_FORBID)


class BoardGeometryError(ValueError):
    """``boundaries`` describes a shape that cannot be built, or two that disagree."""


# --- Shapes -----------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """One piece of a closed path.

    ``line``: (start, end). ``arc``: (start, mid, end), which is how KiCad
    stores an arc and survives rotation unchanged. ``circle``: (center,) with
    ``radius`` set — a circle is its own closed path.
    """

    kind: str
    points: tuple[Point, ...]
    radius: float = 0.0


@dataclass
class Shape:
    segments: list[Segment]
    #: The same boundary as straight lines, counter-clockwise or not —
    #: containment does not care which way round it runs.
    polygon: list[Point]
    #: True for an axis-aligned rectangle with square corners. The placer keeps
    #: its exact arithmetic for these, so every existing board places as before.
    axis_rect: bool = False

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """The true extent, from the curves themselves.

        Not from ``polygon``: the chords of a flattened circle sit inside it,
        so a 40 mm circle measured that way started at (0.02, 0.005) and the
        outline check refused a board drawn exactly as specified.
        """
        xs: list[float] = []
        ys: list[float] = []
        for s in self.segments:
            if s.kind == "circle":
                (cx, cy), r = s.points[0], s.radius
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
            elif s.kind == "line":
                xs += [p[0] for p in s.points]
                ys += [p[1] for p in s.points]
            else:
                a, m, b = s.points
                (cx, cy), r = _arc_center(a, m, b)
                pts = [a, b]
                # An arc reaches past its endpoints wherever it crosses an axis.
                for t in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
                    q = (cx + r * math.cos(t), cy + r * math.sin(t))
                    if _on_arc(q, a, m, b, (cx, cy)):
                        pts.append(q)
                xs += [p[0] for p in pts]
                ys += [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class Feature:
    """A cutout or a keepout: a shape plus what it means."""

    kind: str                     # "cutout" | "keepout"
    shape: Shape
    where: str                    # "boundaries.keepouts[2]", for messages
    anchor: str | None = None
    layers: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()
    name: str = ""

    @property
    def blocks_parts(self) -> bool:
        """Whether a footprint may not sit here.

        A cutout is a hole in the board. A keepout blocks parts when it forbids
        footprints or pads; one forbidding only tracks is a routing rule, and a
        part may sit on it.
        """
        return self.kind == "cutout" or bool({"footprints", "pads"} & set(self.forbid))

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "where": self.where,
            "polygon": [[round(x, 4), round(y, 4)] for x, y in self.shape.polygon],
        }
        if self.anchor:
            out["anchor"] = self.anchor
        if self.kind == "keepout":
            out["layers"] = list(self.layers)
            out["forbid"] = list(self.forbid)
        if self.name:
            out["name"] = self.name
        return out


@dataclass
class Board:
    outline: Shape
    #: Fixed in board coordinates.
    features: list[Feature] = field(default_factory=list)
    #: In their anchor's footprint frame, waiting for a placement.
    anchored: list[Feature] = field(default_factory=list)
    #: Whether the outline came from ``boundaries.board_outline`` rather than
    #: from ``project.dimensions``.
    declared: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def size(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.outline.bbox
        return (x1 - x0, y1 - y0)

    @property
    def extent(self) -> tuple[float, float]:
        """Largest x and y the outline reaches — the placer's search area."""
        _, _, x1, y1 = self.outline.bbox
        return (x1, y1)

    def anchored_to(self, ref: str) -> list[Feature]:
        return [f for f in self.anchored if f.anchor == ref]


def _num(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BoardGeometryError(f"{where} must be a number, not {value!r}")
    if not math.isfinite(value):
        raise BoardGeometryError(f"{where} must be finite")
    return float(value)


def _pos(value: Any, where: str) -> float:
    v = _num(value, where)
    if v <= 0:
        raise BoardGeometryError(f"{where} must be greater than 0, not {value!r}")
    return v


def _point(value: Any, where: str) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise BoardGeometryError(f"{where} must be a point [x, y], not {value!r}")
    return (_num(value[0], f"{where}[0]"), _num(value[1], f"{where}[1]"))


def _lines(points: list[Point]) -> list[Segment]:
    n = len(points)
    return [Segment("line", (points[i], points[(i + 1) % n])) for i in range(n)]


def _arc_center(a: Point, m: Point, b: Point) -> tuple[Point, float]:
    """Centre and radius of the circle through three points."""
    ax, ay = a
    bx, by = m
    cx, cy = b
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < EPS:
        raise BoardGeometryError("an arc's three points are collinear")
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
          + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
          + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy), math.hypot(ax - ux, ay - uy)


def _sweep(a: Point, m: Point, b: Point, c: Point) -> tuple[float, float]:
    """Start angle and signed sweep of the arc a→m→b about centre c."""
    t0 = math.atan2(a[1] - c[1], a[0] - c[0])
    tm = math.atan2(m[1] - c[1], m[0] - c[0])
    t1 = math.atan2(b[1] - c[1], b[0] - c[0])

    def ccw(frm: float, to: float) -> float:
        return (to - frm) % (2 * math.pi)

    # Go whichever way round passes through the midpoint.
    return t0, (ccw(t0, t1) if ccw(t0, tm) <= ccw(t0, t1) else -ccw(t1, t0))


def _on_arc(q: Point, a: Point, m: Point, b: Point, c: Point) -> bool:
    t0, sweep = _sweep(a, m, b, c)
    tq = math.atan2(q[1] - c[1], q[0] - c[0])
    d = (tq - t0) % (2 * math.pi) if sweep >= 0 else (t0 - tq) % (2 * math.pi)
    return d <= abs(sweep) + 1e-12


def _arc_steps(radius: float, sweep: float) -> int:
    """Chords needed to stay within ARC_TOLERANCE_MM of an arc."""
    if radius <= ARC_TOLERANCE_MM:
        return 1
    per = 2 * math.acos(max(-1.0, 1 - ARC_TOLERANCE_MM / radius))
    return max(2, math.ceil(abs(sweep) / per))


def _flatten_arc(a: Point, m: Point, b: Point) -> list[Point]:
    """Points from ``a`` up to (not including) ``b`` along the arc through ``m``."""
    (cx, cy), r = _arc_center(a, m, b)
    t0, sweep = _sweep(a, m, b, (cx, cy))
    n = _arc_steps(r, sweep)
    return [(cx + r * math.cos(t0 + sweep * i / n), cy + r * math.sin(t0 + sweep * i / n))
            for i in range(n)]


def _flatten(segments: list[Segment]) -> list[Point]:
    out: list[Point] = []
    for s in segments:
        if s.kind == "circle":
            (cx, cy), r = s.points[0], s.radius
            n = max(16, _arc_steps(r, 2 * math.pi))
            return [(cx + r * math.cos(2 * math.pi * i / n),
                     cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
        if s.kind == "line":
            out.append(s.points[0])
        else:
            out.extend(_flatten_arc(*s.points))
    return out


def _fillet(points: list[Point], radius: float, where: str) -> list[Segment]:
    """A closed polygon with every corner rounded to ``radius``.

    Each corner is replaced by an arc tangent to both edges. The tangent points
    sit ``r / tan(phi / 2)`` back along each edge, where ``phi`` is the angle
    between the edges at that corner; if two neighbouring corners need more of
    an edge than it has, the radius is too big for the shape and that is said
    rather than drawn as a self-crossing outline.
    """
    n = len(points)
    cuts: list[tuple[Point, Point, Point] | None] = []
    backs: list[float] = []
    for i in range(n):
        p = points[i]
        a, b = points[i - 1], points[(i + 1) % n]
        ua = (a[0] - p[0], a[1] - p[1])
        ub = (b[0] - p[0], b[1] - p[1])
        la, lb = math.hypot(*ua), math.hypot(*ub)
        ua, ub = (ua[0] / la, ua[1] / la), (ub[0] / lb, ub[1] / lb)
        cos_phi = max(-1.0, min(1.0, ua[0] * ub[0] + ua[1] * ub[1]))
        phi = math.acos(cos_phi)
        if abs(math.pi - phi) < 1e-9:      # straight through: nothing to round
            cuts.append(None)
            backs.append(0.0)
            continue
        d = radius / math.tan(phi / 2)
        bis = (ua[0] + ub[0], ua[1] + ub[1])
        bl = math.hypot(*bis)
        bis = (bis[0] / bl, bis[1] / bl)
        h = radius / math.sin(phi / 2)
        center = (p[0] + bis[0] * h, p[1] + bis[1] * h)
        t1 = (p[0] + ua[0] * d, p[1] + ua[1] * d)
        t2 = (p[0] + ub[0] * d, p[1] + ub[1] * d)
        mid = (center[0] - bis[0] * radius, center[1] - bis[1] * radius)
        cuts.append((t1, mid, t2))
        backs.append(d)
    for i in range(n):
        j = (i + 1) % n
        length = math.dist(points[i], points[j])
        if backs[i] + backs[j] > length + EPS:
            raise BoardGeometryError(
                f"{where}.corner_radius {radius:g} is too large: the edge from "
                f"{list(points[i])} to {list(points[j])} is {length:.3f} mm and its two "
                f"rounded corners need {backs[i] + backs[j]:.3f} mm of it"
            )
    segs: list[Segment] = []
    for i in range(n):
        j = (i + 1) % n
        cut_i, cut_j = cuts[i], cuts[j]
        start = cut_i[2] if cut_i else points[i]
        end = cut_j[0] if cut_j else points[j]
        if cut_i:
            segs.append(Segment("arc", cut_i))
        if math.dist(start, end) > EPS:
            segs.append(Segment("line", (start, end)))
    return segs


def _polygon_shape(points: list[Point], radius: float, where: str) -> Shape:
    if len(points) < 3:
        raise BoardGeometryError(f"{where} needs at least 3 points, not {len(points)}")
    for i in range(len(points)):
        if math.dist(points[i], points[(i + 1) % len(points)]) < EPS:
            raise BoardGeometryError(f"{where} repeats the point {list(points[i])}")
    # Crossing first: a bowtie's two lobes cancel, so its signed area is zero
    # and "encloses no area" would describe the symptom rather than the fault.
    crossing = _self_intersection(points)
    if crossing is not None:
        i, j = crossing
        raise BoardGeometryError(
            f"{where} crosses itself: edge {i} ({list(points[i])} → "
            f"{list(points[(i + 1) % len(points)])}) meets edge {j}"
        )
    if abs(_signed_area(points)) < EPS:
        raise BoardGeometryError(f"{where} encloses no area")
    segs = _fillet(points, radius, where) if radius > 0 else _lines(points)
    return Shape(segs, _flatten(segs))


def shape(spec: Any, where: str, *, default: str = "center") -> Shape:
    """Build one shape from its YAML mapping.

    ``default`` says where a rect or circle sits when the spec does not say:
    ``"origin"`` puts its bounding box's top-left at (0, 0), which is what a
    board outline wants; ``"center"`` centres it on (0, 0), which is what a
    shape in a footprint's frame wants.
    """
    if not isinstance(spec, dict):
        raise BoardGeometryError(f"{where} must be a mapping with a 'shape'")
    kind = spec.get("shape")
    radius = _num(spec.get("corner_radius", 0), f"{where}.corner_radius")
    if radius < 0:
        raise BoardGeometryError(f"{where}.corner_radius cannot be negative")

    if kind == "rect":
        size = spec.get("size")
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            raise BoardGeometryError(f"{where}.size must be [width, height]")
        w, h = _pos(size[0], f"{where}.size[0]"), _pos(size[1], f"{where}.size[1]")
        if "origin" in spec and "center" in spec:
            raise BoardGeometryError(f"{where} gives both origin and center; give one")
        if "origin" in spec:
            x0, y0 = _point(spec["origin"], f"{where}.origin")
        elif "center" in spec:
            cx, cy = _point(spec["center"], f"{where}.center")
            x0, y0 = cx - w / 2, cy - h / 2
        elif default == "origin":
            x0, y0 = 0.0, 0.0
        else:
            x0, y0 = -w / 2, -h / 2
        if radius > min(w, h) / 2 + EPS:
            raise BoardGeometryError(
                f"{where}.corner_radius {radius:g} is more than half of the shorter side "
                f"({min(w, h):g} mm)"
            )
        pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
        if radius > 0:
            return _polygon_shape(pts, radius, where)
        segs = _lines(pts)
        return Shape(segs, list(pts), axis_rect=True)

    if kind == "circle":
        if "diameter" in spec and "radius" in spec:
            raise BoardGeometryError(f"{where} gives both diameter and radius; give one")
        if "diameter" in spec:
            r = _pos(spec["diameter"], f"{where}.diameter") / 2
        elif "radius" in spec:
            r = _pos(spec["radius"], f"{where}.radius")
        else:
            raise BoardGeometryError(f"{where} is a circle with no diameter")
        if "origin" in spec and "center" in spec:
            raise BoardGeometryError(f"{where} gives both origin and center; give one")
        if "center" in spec:
            c = _point(spec["center"], f"{where}.center")
        elif "origin" in spec:
            # The top-left of its bounding box, as for every other shape.
            ox, oy = _point(spec["origin"], f"{where}.origin")
            c = (ox + r, oy + r)
        else:
            c = (r, r) if default == "origin" else (0.0, 0.0)
        segs = [Segment("circle", (c,), radius=r)]
        return Shape(segs, _flatten(segs))

    if kind == "polygon":
        # Its points say where it is. An origin or centre beside them could only
        # be ignored or fought with, and ignoring a position is how a shape ends
        # up somewhere other than where it was written.
        for key in ("origin", "center"):
            if key in spec:
                raise BoardGeometryError(
                    f"{where} is a polygon, whose points are its position; drop {key}")
        raw = spec.get("points")
        if not isinstance(raw, list):
            raise BoardGeometryError(f"{where}.points must be a list of [x, y]")
        pts = [_point(p, f"{where}.points[{i}]") for i, p in enumerate(raw)]
        return _polygon_shape(pts, radius, where)

    if kind == "trapezoid":
        bottom = _pos(spec.get("bottom"), f"{where}.bottom")
        top = _pos(spec.get("top"), f"{where}.top")
        height = _pos(spec.get("height"), f"{where}.height")
        off = (_num(spec["top_offset"], f"{where}.top_offset")
               if "top_offset" in spec else (bottom - top) / 2)
        # y is down, so the "top" edge is at y = 0 and the "bottom" at height.
        left = min(0.0, off)
        pts = [(off - left, 0.0), (off + top - left, 0.0),
               (bottom - left, height), (-left, height)]
        if "origin" in spec:
            ox, oy = _point(spec["origin"], f"{where}.origin")
        elif "center" in spec or default != "origin":
            cx, cy = _point(spec.get("center", [0, 0]), f"{where}.center")
            width = max(p[0] for p in pts)
            ox, oy = cx - width / 2, cy - height / 2
        else:
            ox, oy = 0.0, 0.0
        pts = [(x + ox, y + oy) for x, y in pts]
        built = _polygon_shape(pts, radius, where)
        if default == "origin" and "origin" not in spec and "center" not in spec:
            # Rounding an acute corner pulls the shape in from the corner it
            # rounds: a 60/40/30 trapezoid with r=2 starts at x=0.775, not 0.
            # Nobody placing it at the origin means that, so it is moved back.
            x0, y0, _, _ = built.bbox
            built = transform(built, -x0, -y0, 0.0)
        return built

    raise BoardGeometryError(
        f"{where}.shape must be one of rect, circle, polygon, trapezoid — not {kind!r}"
    )


def transform(s: Shape, x: float, y: float, rot: float) -> Shape:
    """``s`` in a footprint's frame, as it lands on the board at (x, y, rot).

    The same mapping ``placement.rotated_geometry`` measured with pcbnew: a
    footprint orientation of +90 deg sends a local offset (dx, dy) to
    (dy, -dx). Arcs and circles are carried by their defining points, which
    rotation and translation preserve.
    """
    a = math.radians(float(rot) % 360.0)
    c, sn = math.cos(a), math.sin(a)

    def t(p: Point) -> Point:
        return (x + p[0] * c + p[1] * sn, y - p[0] * sn + p[1] * c)

    segs = [Segment(seg.kind, tuple(t(p) for p in seg.points), seg.radius)
            for seg in s.segments]
    axis = s.axis_rect and abs(round(float(rot) % 90.0, 9)) < 1e-9
    return Shape(segs, [t(p) for p in s.polygon], axis_rect=axis)


# --- Predicates ---------------------------------------------------------------


def _signed_area(poly: list[Point]) -> float:
    return sum(poly[i][0] * poly[(i + 1) % len(poly)][1]
               - poly[(i + 1) % len(poly)][0] * poly[i][1] for i in range(len(poly))) / 2


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segments_cross(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """Whether two segments intersect anywhere, touching included."""
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    if ((d1 > EPS and d2 < -EPS) or (d1 < -EPS and d2 > EPS)) and \
       ((d3 > EPS and d4 < -EPS) or (d3 < -EPS and d4 > EPS)):
        return True

    def on(p: Point, q: Point, r: Point) -> bool:
        return (min(p[0], q[0]) - EPS <= r[0] <= max(p[0], q[0]) + EPS
                and min(p[1], q[1]) - EPS <= r[1] <= max(p[1], q[1]) + EPS)

    return ((abs(d1) <= EPS and on(q1, q2, p1)) or (abs(d2) <= EPS and on(q1, q2, p2))
            or (abs(d3) <= EPS and on(p1, p2, q1)) or (abs(d4) <= EPS and on(p1, p2, q2)))


def _self_intersection(poly: list[Point]) -> tuple[int, int] | None:
    n = len(poly)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue                      # neighbours share a vertex
            if _segments_cross(poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n]):
                return (i, j)
    return None


def point_in_polygon(p: Point, poly: list[Point]) -> bool:
    """Ray casting. A point exactly on the boundary may go either way."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _segment_enters_box(a: Point, b: Point, box: tuple[float, float, float, float]) -> bool:
    """Whether segment a-b passes through the box's *open* interior.

    Liang-Barsky clipping to the closed box, then the midpoint of what is left
    tested strictly. A segment lying along the box's own edge clips to that
    edge, whose midpoint is not interior — so a part flush against a straight
    board edge is allowed, exactly as the rectangle arithmetic allowed it.
    """
    x0, y0, x1, y1 = box
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if abs(p) < 1e-15:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return False
    tm = (t0 + t1) / 2
    mx, my = a[0] + dx * tm, a[1] + dy * tm
    return x0 + EPS < mx < x1 - EPS and y0 + EPS < my < y1 - EPS


def box_inside(box: tuple[float, float, float, float], poly: list[Point]) -> bool:
    """Whether the box lies entirely inside the polygon (touching its edge allowed)."""
    x0, y0, x1, y1 = box
    n = len(poly)
    for i in range(n):
        if _segment_enters_box(poly[i], poly[(i + 1) % n], box):
            return False
    # No edge crosses the interior, so the box is wholly in or wholly out.
    return point_in_polygon(((x0 + x1) / 2, (y0 + y1) / 2), poly)


def box_overlaps(box: tuple[float, float, float, float], poly: list[Point]) -> bool:
    """Whether the box's interior and the polygon's interior share any area."""
    x0, y0, x1, y1 = box
    n = len(poly)
    for i in range(n):
        if _segment_enters_box(poly[i], poly[(i + 1) % n], box):
            return True
    # No boundary crossing: one contains the other, or they are apart.
    if point_in_polygon(((x0 + x1) / 2, (y0 + y1) / 2), poly):
        return True
    px, py = poly[0]
    return x0 < px < x1 and y0 < py < y1


def distance_to_boundary(p: Point, poly: list[Point]) -> float:
    best = math.inf
    n = len(poly)
    for i in range(n):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        t = 0.0 if ll == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / ll))
        best = min(best, math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy)))
    return best


def polygon_inside(inner: list[Point], outer: list[Point]) -> bool:
    """Whether ``inner`` lies entirely within ``outer``, not touching its edge."""
    if not all(point_in_polygon(p, outer) for p in inner):
        return False
    n, m = len(inner), len(outer)
    return not any(
        _segments_cross(inner[i], inner[(i + 1) % n], outer[j], outer[(j + 1) % m])
        for i in range(n) for j in range(m)
    )


def polygons_overlap(a: list[Point], b: list[Point]) -> bool:
    n, m = len(a), len(b)
    if any(_segments_cross(a[i], a[(i + 1) % n], b[j], b[(j + 1) % m])
           for i in range(n) for j in range(m)):
        return True
    return point_in_polygon(a[0], b) or point_in_polygon(b[0], a)


# --- project.yaml -------------------------------------------------------------


def _feature(kind: str, spec: Any, where: str) -> Feature:
    if not isinstance(spec, dict):
        raise BoardGeometryError(f"{where} must be a mapping with a 'shape'")
    anchor = spec.get("anchor")
    if anchor is not None and (not isinstance(anchor, str) or not anchor.strip()):
        raise BoardGeometryError(f"{where}.anchor must be a refdes")
    s = shape(spec, where, default="center")
    layers: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()
    if kind == "keepout":
        raw_layers = spec.get("layers", ["*.Cu"])
        if isinstance(raw_layers, str):
            raw_layers = [raw_layers]
        if not isinstance(raw_layers, list) or not raw_layers or \
                not all(isinstance(x, str) and x for x in raw_layers):
            raise BoardGeometryError(f"{where}.layers must be a list of layer names")
        layers = tuple(raw_layers)
        raw_forbid = spec.get("forbid", list(DEFAULT_FORBID))
        if not isinstance(raw_forbid, list) or not raw_forbid:
            raise BoardGeometryError(
                f"{where}.forbid must list one or more of {', '.join(DEFAULT_FORBID)}")
        bad = [f for f in raw_forbid if f not in FORBIDDABLE]
        if bad:
            raise BoardGeometryError(
                f"{where}.forbid has {bad}; allowed: {', '.join(DEFAULT_FORBID)}")
        forbid = tuple(f for f in DEFAULT_FORBID if f in raw_forbid)
    return Feature(kind, s, where, anchor=anchor.strip() if anchor else None,
                   layers=layers, forbid=forbid, name=str(spec.get("name") or ""))


def from_config(config: dict) -> Board:
    """The board ``project.yaml`` describes, checked, or a BoardGeometryError.

    Two outline forms exist. The **shape** form (``board_outline.shape``) is
    authoritative, and ``project.dimensions`` must then equal its bounding box:
    two numbers describing one board are either the same or one of them is
    wrong, and picking a winner quietly is how a board ships at the wrong size.

    The **legacy** form (``type: rect`` with ``start``/``end``) is what
    ``blpl init`` has always written. It was never read, so it never decided
    anything, and it is not promoted to deciding things now: the app's board
    panel edits ``dimensions`` and leaves it alone, so on a board whose size
    was changed there it is simply stale. ``dimensions`` keeps deciding, and a
    stale legacy outline is reported in ``notes`` rather than obeyed.
    """
    dims = (config.get("project") or {}).get("dimensions") or []
    try:
        dw, dh = float(dims[0]), float(dims[1])
    except (IndexError, TypeError, ValueError):
        raise BoardGeometryError("project.dimensions must be [width, height]") from None

    boundaries = config.get("boundaries") or {}
    if not isinstance(boundaries, dict):
        raise BoardGeometryError("boundaries must be a mapping")
    raw_outline = boundaries.get("board_outline") or {}
    notes: list[str] = []

    # Only an absent outline and the legacy forms may fall back to the
    # dimensions rectangle. The schema requires neither 'shape' nor 'type' and
    # allows extra keys, so a typo such as 'shpae: circle' used to pass it and
    # build a rectangle the text does not describe, with nothing said.
    if not isinstance(raw_outline, dict):
        raise BoardGeometryError(
            f"boundaries.board_outline must be a mapping with a 'shape', "
            f"not {type(raw_outline).__name__} {raw_outline!r}"
        )
    if raw_outline and "shape" not in raw_outline and raw_outline.get("type") not in (
            "rect", "polygon"):
        keys = ", ".join(sorted(str(k) for k in raw_outline))
        legacy = (f" and the legacy 'type' is {raw_outline['type']!r}, not 'rect'"
                  if "type" in raw_outline else "")
        raise BoardGeometryError(
            f"boundaries.board_outline has no 'shape'{legacy} (it gives: {keys}). "
            "Describe the outline with 'shape:' (rect, circle, trapezoid or polygon), "
            "or remove the block to use project.dimensions."
        )

    if "shape" in raw_outline:
        outline = shape(raw_outline, "boundaries.board_outline", default="origin")
        x0, y0, x1, y1 = outline.bbox
        if abs((x1 - x0) - dw) > 0.01 or abs((y1 - y0) - dh) > 0.01:
            raise BoardGeometryError(
                f"project.dimensions is {dw:g} x {dh:g} mm but boundaries.board_outline "
                f"spans {x1 - x0:.3f} x {y1 - y0:.3f} mm. With an outline shape, dimensions "
                f"must be its bounding box — set dimensions to "
                f"[{round(x1 - x0, 3):g}, {round(y1 - y0, 3):g}]."
            )
        if x0 < -0.001 or y0 < -0.001:
            # Placement, hints and the emitter's sheet offset all share one
            # frame, whose origin is the top-left of KiCad's working area.
            # Negative coordinates put the board outside it.
            raise BoardGeometryError(
                f"boundaries.board_outline reaches ({x0:.3f}, {y0:.3f}); no point of the "
                "outline may be left of x = 0 or above y = 0"
            )
        declared = True
    else:
        if raw_outline.get("type") == "rect":
            start, end = raw_outline.get("start"), raw_outline.get("end")
            try:
                lw = float(end[0]) - float(start[0])
                lh = float(end[1]) - float(start[1])
                if abs(lw - dw) > 0.01 or abs(lh - dh) > 0.01:
                    notes.append(
                        f"boundaries.board_outline (legacy type: rect) is {lw:g} x {lh:g} mm "
                        f"but project.dimensions is {dw:g} x {dh:g}; dimensions decides. "
                        "Replace the legacy block with 'shape: rect' and 'size' to remove "
                        "the ambiguity."
                    )
            except (TypeError, ValueError, IndexError):
                pass
        elif raw_outline.get("type") == "polygon":
            # The schema has accepted this since the first version and the
            # emitter has always drawn a rectangle anyway. Refusing is the
            # honest answer now that a polygon can actually be drawn.
            raise BoardGeometryError(
                "boundaries.board_outline uses the old 'type: polygon', which was never "
                "drawn. Write it as 'shape: polygon' with 'points: [[x, y], ...]'."
            )
        outline = shape({"shape": "rect", "size": [dw, dh]}, "project.dimensions",
                        default="origin")
        declared = False

    board = Board(outline=outline, declared=declared, notes=notes)
    for kind, key in (("cutout", "cutouts"), ("keepout", "keepouts")):
        raw = boundaries.get(key) or []
        if not isinstance(raw, list):
            raise BoardGeometryError(f"boundaries.{key} must be a list")
        for i, spec in enumerate(raw):
            f = _feature(kind, spec, f"boundaries.{key}[{i}]")
            (board.anchored if f.anchor else board.features).append(f)

    for f in board.features:
        check_feature(board, f)
    return board


def check_feature(board: Board, f: Feature) -> None:
    """A cutout must be a hole, wholly inside the board.

    A cutout that crosses the outline is not a hole, it is a notch — and two
    Edge.Cuts shapes that overlap are a malformed board KiCad refuses to fill
    zones on. A notch belongs in the outline itself (a polygon), so that is
    what the error says. Keepouts may run off the board: KiCad clips a rule
    area to the outline, and an antenna keepout often does exactly that.
    """
    if f.kind != "cutout":
        return
    if not polygon_inside(f.shape.polygon, board.outline.polygon):
        anchored = f" (anchored to {f.anchor})" if f.anchor else ""
        raise BoardGeometryError(
            f"{f.where}{anchored} is not wholly inside the board outline. A cutout is a "
            "hole; a notch in the edge belongs in board_outline as a polygon."
        )
    for other in board.features:
        if other is not f and other.kind == "cutout" and \
                polygons_overlap(f.shape.polygon, other.shape.polygon):
            raise BoardGeometryError(f"{f.where} overlaps {other.where}")


def place_anchored(board: Board, ref: str, x: float, y: float, rot: float,
                   side: str = "top") -> list[Feature]:
    """Fix ``ref``'s anchored shapes at its placement, check them, and return them.

    A bottom-side part is refused rather than mirrored. KiCad mirrors a
    flipped footprint's frame, so mirroring is what an anchored shape *should*
    do — but the emitter does not flip footprints yet (it relabels the
    footprint B.Cu and leaves its pads unmirrored on F.Cu), and a mirrored
    keepout beside an unmirrored part is on the wrong side of it either way.
    Until the two agree, saying so beats drawing the antenna keepout over the
    wrong end of the module.
    """
    if str(side).lower() == "bottom" and board.anchored_to(ref):
        raise BoardGeometryError(
            f"{ref} is on the bottom side and has anchored shapes "
            f"({', '.join(f.where for f in board.anchored_to(ref))}); anchoring to a "
            "bottom-side part is not supported yet, because the emitter does not mirror "
            "flipped footprints. Draw the shape in board coordinates instead"
        )
    placed = []
    for f in board.anchored_to(ref):
        g = Feature(f.kind, transform(f.shape, x, y, rot), f.where, anchor=f.anchor,
                    layers=f.layers, forbid=f.forbid, name=f.name)
        check_feature(board, g)
        board.features.append(g)
        placed.append(g)
    return placed


def as_record(board: Board) -> dict[str, Any]:
    """What Stage 5 writes into the HDM: the resolved geometry, in board coordinates.

    The emitter draws from this rather than re-deriving it from project.yaml,
    because anchored shapes only have a position once Stage 5 has placed their
    part. Stage 8 checks against it for the same reason.
    """
    out: dict[str, Any] = {
        "outline": {
            "declared": board.declared,
            "segments": [_segment_record(s) for s in board.outline.segments],
            "polygon": [[round(x, 4), round(y, 4)] for x, y in board.outline.polygon],
        },
        "features": [
            {**f.as_dict(), "segments": [_segment_record(s) for s in f.shape.segments]}
            for f in board.features
        ],
    }
    unresolved = sorted({f.anchor for f in board.anchored} - {f.anchor for f in board.features})
    if unresolved:
        out["unresolved_anchors"] = unresolved
    if board.notes:
        out["notes"] = list(board.notes)
    return out


def _segment_record(s: Segment) -> dict[str, Any]:
    rec: dict[str, Any] = {"kind": s.kind,
                           "points": [[round(x, 6), round(y, 6)] for x, y in s.points]}
    if s.kind == "circle":
        rec["radius"] = round(s.radius, 6)
    return rec


def segments_from_record(raw: list[dict]) -> list[Segment]:
    return [Segment(str(r["kind"]), tuple((float(p[0]), float(p[1])) for p in r["points"]),
                    float(r.get("radius", 0.0))) for r in raw]
