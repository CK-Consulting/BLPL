"""Where every symbol, wire, label and power symbol goes on the schematic sheet.

The sheet used to be a grid: every part in a cell of the same size, every pin
a short stub and a local label, in roughly board order. Electrically it was
complete; as a drawing it was "a grid of parts", useless to the outside firm
that has to review a design from its schematic. Two things made it so, and
this module answers both.

**Parts that serve one IC are drawn with it.** Each part with more than two
pins is an *anchor* and gets a framed block. Every one- and two-pin part
joins the block of the anchor it serves: the one it shares a signal with,
or for a decoupling capacitor (rails only) the one with a pin on that rail,
nearest on the board. What is left over gets a block of its own.

**Parts are drawn the way an engineer draws them.** A two-pin part is turned
so its supply pin points up and its ground pin down; a series part lies on
its side, signal in on the left and out on the right. Supply rails end in a
power symbol named for the rail, ground in a ground symbol, and every other
net in a global label. Global, not local: a local label's net is
``/NAME`` while the board's is ``NAME``, and that difference alone was 87 of
verdacor-ear-tag's schematic parity errors. A power symbol's net is its
Value (checked against kicad-cli's netlist), so a rail called VDD_LR2021
needs no library symbol of its own.

**Parts that serve a pin are wired to it.** Grouping alone still left a
sheet of parts and labels with not one wire between them. A small part that
shares a net with one of its anchor's pins hangs off a wire drawn from that
pin (see ``_hang``): below the wire on a side edge, beside it on a top or
bottom one, further parts further out. The wire ends in a global label, which
joins the net to the rest of the sheet and names it as the board does.

Geometry is in schematic millimetres, y down, on the 1.27 mm connection
grid. Symbols are defined y-up; ``pin_tip`` does that flip and the rotation.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

GRID = 1.27
STUB = 2 * GRID
_CHAR = 1.27  # stroke font advance at 1.27 mm, rounded up
_POWER_HEIGHT = 5.08  # a power symbol's body, beyond its pin

GROUND = re.compile(r"^(A|D|P|S|CHASSIS_)?GND\w*$|^VSS\w*$|^0V$", re.IGNORECASE)
RAIL = re.compile(
    r"^(\+?\d+V\d*\w*|V(DD|CC|BAT|BUS|IN|SYS|IO|REF|CORE|DDIO|CCIO|_)\w*|AVDD\w*|DVDD\w*|VBATT?\w*|VPP\w*|VLED\w*)$",
    re.IGNORECASE,
)


def snap(v: float) -> float:
    return round(v / GRID) * GRID


def net_kind(name: str) -> str:
    """``ground``, ``rail`` or ``signal``: what a net ends in on the sheet."""
    if GROUND.match(name):
        return "ground"
    if RAIL.match(name):
        return "rail"
    return "signal"


def _rot(vx: float, vy: float, deg: float) -> tuple[float, float]:
    """A sheet vector (y down) turned counter-clockwise as seen, by ``deg``."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return (vx * c + vy * s, -vx * s + vy * c)


def _angle(vx: float, vy: float) -> int:
    """The nearest of 0/90/180/270 (as seen, counter-clockwise from +x)."""
    return int(round(math.degrees(math.atan2(-vy, vx)) / 90.0)) % 4 * 90


def pin_tip(pin: dict, x: float, y: float, rot: float) -> tuple[float, float, int]:
    """Where a pin connects on the sheet, and the direction it points away from its body."""
    dx, dy = _rot(pin["x"], -pin["y"], rot)
    a = math.radians(pin["rot"])
    ox, oy = _rot(-math.cos(a), math.sin(a), rot)
    return (snap(x + dx), snap(y + dy), _angle(ox, oy))


_STEP = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}
# Landscape sheets, smallest first, as sch._paper chooses among them.
SHEETS = ((297.0, 210.0), (420.0, 297.0), (594.0, 420.0), (841.0, 594.0), (1189.0, 841.0))


@dataclass
class Placed:
    ref: str
    unit: int
    x: float
    y: float
    rot: int
    pins: list[dict]
    # A small part drawn hanging off one of its anchor's pins: (anchor ref,
    # anchor unit, anchor pin number, this part's pin number on that wire).
    hook: tuple[str, int, str, str] | None = None


@dataclass
class WirePath:
    """The wire from an anchor pin through the parts that hang off it.

    ``points`` are its corners and its taps, in order from the pin; ``taps``
    are the points a part's pin joins it; the label naming its net is drawn at
    ``label``, pointing ``direction``.
    """
    points: list[tuple[float, float]]
    taps: set[tuple[float, float]]
    label: tuple[float, float]
    direction: int

    def moved(self, dx: float, dy: float) -> "WirePath":
        def mv(p):
            return (snap(p[0] + dx), snap(p[1] + dy))
        return WirePath([mv(p) for p in self.points], {mv(p) for p in self.taps}, mv(self.label), self.direction)


# Parts on one row of a pin's wire before it folds back on itself. Seven parts
# on VBAT in one straight line stretched the XIAO's block down half the sheet.
ROW_PARTS = 3


@dataclass
class Block:
    title: str
    anchor: str | None
    members: list[tuple[str, int]] = field(default_factory=list)
    # Filled by layout: the block's own frame, in sheet coordinates.
    # wire_paths: for each anchor pin that has parts hanging off it, (ref,
    # unit, pin number) -> WirePath, in sheet coordinates.
    wire_paths: dict = field(default_factory=dict)
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0


def _two_pin_rotation(pins: list[dict], nets: dict[str, str]) -> int:
    """The rotation that draws a small part the conventional way round.

    Wanted directions by what each pin's net ends in: a rail points up,
    ground down, and a signal up when the other pin is ground or down when it
    is a rail. Two signals (a series part), or two rails (a filter, a
    blocking diode), lie on their side with pin 1 on the left.
    """
    kinds = [net_kind(nets[p["number"]]) if p["number"] in nets else "none" for p in pins]
    want: dict[str, int] = {}
    if len(pins) == 1:
        want[pins[0]["number"]] = 90
    elif len(pins) == 2:
        a, b = pins
        ka, kb = kinds
        order = {"rail": 0, "signal": 1, "none": 1, "ground": 2}
        if ka == kb or {ka, kb} <= {"signal", "none"}:
            want = {a["number"]: 180, b["number"]: 0}
        elif order[ka] < order[kb]:
            want = {a["number"]: 90, b["number"]: 270}
        else:
            want = {a["number"]: 270, b["number"]: 90}
    best, score = 0, -1
    for rot in (0, 90, 180, 270):
        s = sum(1 for p in pins if pin_tip(p, 0, 0, rot)[2] == want.get(p["number"], -1))
        if s > score:
            best, score = rot, s
    return best


def ends_in_symbol(net: str, d: int, labels_only: bool) -> bool:
    """Whether a pin pointing ``d`` on ``net`` ends in a power symbol (else a label).

    Only where the symbol reads the right way up — a rail's standing up,
    ground's hanging down — and never on an anchor, whose pins sit closer than
    a power symbol's name is wide. sch.py draws by this rule and the layout
    measures by it, so the two cannot disagree about how much room a pin takes.
    """
    kind = net_kind(net)
    return not labels_only and ((kind == "rail" and d == 90) or (kind == "ground" and d == 270))


def _extent(pins: list[dict], rot: int, nets: dict[str, str]) -> tuple[float, float, float, float]:
    """The box a placed symbol needs: pins, stubs, and what ends each pin.

    Labels and power symbols are counted, because they are drawn and two
    blocks whose labels overlap read, to KiCad, as connected.
    """
    labels_only = len(pins) > 2
    xs, ys = [0.0], [0.0]
    for p in pins:
        tx, ty, d = pin_tip(p, 0, 0, rot)
        xs.append(tx)
        ys.append(ty)
        net = nets.get(p["number"])
        if not net:
            continue
        sx, sy = _STEP[d]
        if ends_in_symbol(net, d, labels_only):
            reach = STUB + _POWER_HEIGHT
            # Its name is printed level, centred over the symbol.
            half = len(net) * _CHAR / 2
            xs += [tx - half, tx + half]
        else:
            reach = STUB + len(net) * _CHAR + 2 * GRID
        xs.append(tx + sx * reach)
        ys.append(ty + sy * reach)
        # A label is as tall as its text across the direction it runs.
        if sx:
            ys += [ty - 2 * GRID, ty + 2 * GRID]
        else:
            xs += [tx - 2 * GRID, tx + 2 * GRID]
    return (min(xs), min(ys), max(xs), max(ys))


def assign_blocks(
    parts: dict[str, list[tuple[int, list[dict]]]],
    pin_nets: dict[str, dict[str, str]],
    positions: dict[str, tuple[float, float]],
    titles: dict[str, str],
    near: dict[str, str] | None = None,
) -> list[Block]:
    """Group parts into blocks, one per anchor, small parts with what they serve.

    ``parts`` is ref → [(unit, pins)]. An anchor has more than two pins. A
    small part goes, in order of preference, to:

    1. the anchor its Connections row names (``near``: "at U3 pin 8");
    2. the anchor it shares its rarest net with — a signal over a rail, and a
       rail only one anchor uses (FB1's VDD_LR2021) over one every anchor
       uses (VDD_3V3), which by itself says nothing;
    3. of those tied, the nearest on the board.

    Ground never decides anything: every part is on it. A part with nothing
    to go by goes to a last block of its own.
    """
    near = near or {}

    def pin_count(ref: str) -> int:
        return sum(len(pins) for _, pins in parts[ref])

    anchors = [r for r in parts if pin_count(r) > 2]
    anchors.sort(key=lambda r: (-pin_count(r), r))
    nets_of = {r: set(pin_nets.get(r, {}).values()) for r in parts}
    users = {}
    for a in anchors:
        for n in nets_of[a]:
            users[n] = users.get(n, 0) + 1

    def nearest(ref: str, cands: list[str]) -> str:
        if ref in positions:
            px, py = positions[ref]
            with_pos = [c for c in cands if c in positions]
            if with_pos:
                return min(with_pos, key=lambda c: (positions[c][0] - px) ** 2 + (positions[c][1] - py) ** 2)
        return cands[0]

    def score(ref: str, anchor: str) -> float:
        shared = (nets_of[ref] & nets_of[anchor]) - {n for n in nets_of[ref] if net_kind(n) == "ground"}
        return sum((10.0 if net_kind(n) == "signal" else 1.0) / users[n] for n in shared)

    blocks = {a: Block(title=titles.get(a, a), anchor=a, members=[(a, u) for u, _ in parts[a]]) for a in anchors}
    leftover = Block(title="Other parts", anchor=None)
    for ref in parts:
        if ref in blocks:
            continue
        if near.get(ref) in blocks:
            target = blocks[near[ref]]
        else:
            scored = [(score(ref, a), a) for a in anchors]
            best = max((sc for sc, _ in scored), default=0.0)
            cands = [a for sc, a in scored if sc == best and sc > 0]
            target = blocks[nearest(ref, cands)] if cands else leftover
        target.members += [(ref, u) for u, _ in parts[ref]]
    out = [blocks[a] for a in anchors]
    if leftover.members:
        out.append(leftover)
    return out


def single_pins(pins: list[dict], tips: list[tuple[float, float, int]], nets: dict[str, str]) -> set[str]:
    """Numbers of the pins whose neighbours on their edge are on other nets.

    A run of adjacent pins on one net is tied into one terminal (see
    sch._same_net_runs); only a pin on its own can carry a wire of its own.
    """
    by_edge: dict[tuple[int, float], list[tuple[float, str, str | None]]] = {}
    for pin, (tx, ty, d) in zip(pins, tips):
        line = tx if d in (0, 180) else ty
        along = ty if d in (0, 180) else tx
        by_edge.setdefault((d, line), []).append((along, pin["number"], nets.get(pin["number"])))
    out: set[str] = set()
    for edge in by_edge.values():
        edge.sort()
        for i, (_, num, net) in enumerate(edge):
            if net is None:
                continue
            prev = edge[i - 1][2] if i > 0 else None
            nxt = edge[i + 1][2] if i + 1 < len(edge) else None
            if net != prev and net != nxt:
                out.add(num)
    return out


# A part hangs below a wire that runs sideways, and stands to the right of
# one that runs up or down: its pin on the wire points back at the wire.
_FACING = {0: 90, 180: 90, 90: 180, 270: 180}
# The direction a part's pin on the wire points, by the side it hangs (h).
_TOWARD_WIRE = {(0, 1): 90, (0, -1): 270, (1, 0): 180, (-1, 0): 0}


def _hanging_rotation(pins: list[dict], hook_pin: str, facing: int = 90) -> int | None:
    """The rotation that points ``hook_pin`` at its wire (``facing``), the rest away."""
    best = None
    away = (facing + 180) % 360
    for rot in (0, 90, 180, 270):
        tips = {p["number"]: pin_tip(p, 0, 0, rot)[2] for p in pins}
        if tips.get(hook_pin) != facing:
            continue
        if all(d == away for n, d in tips.items() if n != hook_pin):
            return rot
        best = rot if best is None else best
    return best


def choose_hooks(
    anchor_pins: list[tuple[str, int, dict, float, float, int]],
    smalls: list[tuple[str, int, list[dict]]],
    pin_nets: dict[str, dict[str, str]],
) -> dict[tuple[str, int], tuple[tuple[str, int, dict, float, float, int], str]]:
    """Which anchor pin each small part hangs off, and by which of its own pins.

    ``anchor_pins`` are the anchor's single pins: (ref, unit, pin, x, y,
    direction). A part hangs off a pin it shares a net
    with — a signal before a rail, ground never (every part is on it) — so a
    series resistor sits on the line it is in series with and a decoupling
    capacitor on the pin it decouples. One part, one wire: a part on two
    anchor pins hangs off one and labels the other.
    """
    hooks = {}
    for ref, unit, pins in smalls:
        if len(pins) > 2:
            continue
        nets = pin_nets.get(ref, {})
        best = None
        for pin in pins:
            net = nets.get(pin["number"])
            if not net or net_kind(net) == "ground":
                continue
            for ap in anchor_pins:
                if pin_nets.get(ap[0], {}).get(ap[2]["number"]) != net:
                    continue
                if _hanging_rotation(pins, pin["number"], _FACING[ap[5]]) is None:
                    continue
                key = (0 if net_kind(net) == "signal" else 1, ap[4], ap[2]["number"])
                if best is None or key < best[0]:
                    best = (key, ap, pin["number"])
        if best:
            hooks[(ref, unit)] = (best[1], best[2])
    return hooks


def _text_box(texts: tuple[str, str] | None, standing: bool) -> tuple[float, float, float, float]:
    """The room a part's reference and value need beside its body."""
    if not texts:
        return (0.0, 0.0, 0.0, 0.0)
    w = max(len(t) for t in texts) * _CHAR
    if standing:
        return (0.0, -2 * GRID, 2 * GRID + w, 2 * GRID)
    return (-w / 2, -4 * GRID, w / 2, 4 * GRID)


def part_extent(pins: list[dict], rot: int, nets: dict[str, str], texts: tuple[str, str] | None = None):
    """A placed part's whole footprint on the sheet: pins, what ends them, and its text."""
    x0, y0, x1, y1 = _extent(pins, rot, nets)
    if len(pins) <= 2 and pins:
        standing = len({pin_tip(p, 0, 0, rot)[0] for p in pins}) == 1
        tx0, ty0, tx1, ty1 = _text_box(texts, standing)
        x0, y0, x1, y1 = min(x0, tx0), min(y0, ty0), max(x1, tx1), max(y1, ty1)
    return x0, y0, x1, y1


def _hang(local, smalls, pin_nets, texts, pad, order):
    """Hang each small part that serves an anchor pin off a wire from that pin.

    The wire runs straight out from the pin. On a left or right edge each part
    hangs below it; on a top or bottom edge each stands to its right; either
    way its pin is on the wire, and the next part is further out. Pins further
    along the side parts hang towards (lower, or further right) get the space
    nearest the anchor, so no part stands across another pin's wire. Room is
    left after each pin's last part for the label that names its net.

    Returns the parts hung (by (ref, unit)), the right edge of everything hung,
    the boxes of the wires and labels, and where each pin's wire ends.
    """
    anchor_pins = []
    for p in local:
        nets = pin_nets.get(p.ref, {})
        tips = [pin_tip(pin, p.x, p.y, p.rot) for pin in p.pins]
        single = single_pins(p.pins, tips, nets)
        for pin, (tx, ty, d) in zip(p.pins, tips):
            if pin["number"] in single and nets.get(pin["number"]):
                anchor_pins.append((p.ref, p.unit, pin, tx, ty, d))
    hooks = choose_hooks(anchor_pins, smalls, pin_nets)
    by_pin: dict[tuple[str, int, str], list] = {}
    for (ref, unit, pins) in smalls:
        if (ref, unit) in hooks:
            ap, own = hooks[(ref, unit)]
            by_pin.setdefault((ap[0], ap[1], ap[2]["number"]), []).append((ref, unit, pins, own, ap))
    boxes = [(e[0] + p.x, e[1] + p.y, e[2] + p.x, e[3] + p.y)
             for p in local for e in [part_extent(p.pins, p.rot, pin_nets.get(p.ref, {}))]]
    if not boxes:
        return set(), 0.0, [], {}
    bx0, by0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    bx1, by1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    hung, extra, paths = set(), [], {}
    placed_boxes: list[tuple[float, float, float, float]] = []
    mid_x, mid_y = (bx0 + bx1) / 2, (by0 + by1) / 2

    def side(ap) -> tuple[int, int]:
        """Which way a pin's parts hang: away from the middle of its edge.

        Each half of an edge stacks its pins' parts on its own side, so the
        two halves grow apart instead of one stack growing the length of
        both.
        """
        _, _, _, tx, ty, d = ap
        if d in (0, 180):
            return (0, -1) if ty < mid_y else (0, 1)
        return (-1, 0) if tx < mid_x else (1, 0)

    # Sideways edges first; a top or bottom edge then starts beyond whatever
    # those hung, so its parts cannot land on them.
    for edges in ((0, 180), (90, 270)):
        # Each edge's cursor is measured along its wires' direction: how far
        # out from the anchor the next pin's parts start, one per half.
        base = {0: bx1 + pad, 180: -bx0 + pad, 90: -by0 + pad, 270: by1 + pad}
        if edges == (90, 270) and placed_boxes:
            base[90] = max(base[90], -min(b[1] for b in placed_boxes) + pad)
            base[270] = max(base[270], max(b[3] for b in placed_boxes) + pad)
        cursor: dict[tuple[int, tuple[int, int]], float] = {}
        groups = [g for g in by_pin.values() if g[0][4][5] in edges]
        # Furthest along the hanging side first: nearest the anchor.
        def reach(g):
            ap = g[0][4]
            hx, hy = side(ap)
            return -(ap[3] * hx + ap[4] * hy)
        groups.sort(key=reach)
        for group in groups:
            ap = group[0][4]
            _, _, apin, tx, ty, d = ap
            w = _STEP[d]
            h = side(ap)
            key = (d, h)
            cursor.setdefault(key, base[d])

            def ab(x, y, w=w, h=h):
                return (x * w[0] + y * w[1], x * h[0] + y * h[1])

            def xy(a, b, w=w, h=h):
                return (a * w[0] + b * h[0], a * w[1] + b * h[1])

            net = pin_nets.get(ap[0], {}).get(apin["number"], "")
            members = sorted(group, key=lambda g: order(g[0]))
            rows = [members[i:i + ROW_PARTS] for i in range(0, len(members), ROW_PARTS)]
            a_tip, row_b = ab(tx, ty)
            start_a = cursor[key]
            far_a = start_a
            row_taps: list[list[tuple[float, float]]] = []
            row_bs: list[float] = []
            region: list[tuple[float, float, float, float]] = []
            for row in rows:
                cur = start_a
                deepest = row_b
                taps_here = []
                for ref, unit, pins, own, _ in row:
                    rot = _hanging_rotation(pins, own, _TOWARD_WIRE[h])
                    nets = {k: v for k, v in pin_nets.get(ref, {}).items() if k != own}
                    x0, y0, x1, y1 = part_extent(pins, rot, nets, texts.get(ref))
                    corners = [ab(x, y) for x in (x0, x1) for y in (y0, y1)]
                    a_lo, a_hi = min(c[0] for c in corners), max(c[0] for c in corners)
                    b_hi = max(c[1] for c in corners)
                    pin_a, pin_b = ab(*pin_tip(next(q for q in pins if q["number"] == own), 0, 0, rot)[:2])
                    oa, ob = snap(cur - a_lo), row_b - pin_b
                    px, py = xy(oa, ob)
                    px, py = snap(px), snap(py)
                    local.append(Placed(ref, unit, px, py, rot, pins, hook=(ap[0], ap[1], apin["number"], own)))
                    placed_boxes.append((px + x0, py + y0, px + x1, py + y1))
                    hung.add((ref, unit))
                    taps_here.append(xy(oa + pin_a, row_b))
                    cur = oa + a_hi + pad
                    deepest = max(deepest, ob + b_hi)
                    far_a = max(far_a, oa + a_hi)
                row_taps.append(taps_here)
                row_bs.append(row_b)
                row_b = snap(deepest + pad)
            far_a = snap(far_a + pad)
            near_a = snap(start_a - 2 * GRID)
            # The path: out along the first row, then back and forth.
            pts = [(tx, ty)]
            for i, (taps_here, b) in enumerate(zip(row_taps, row_bs)):
                forward = i % 2 == 0
                ordered = sorted(taps_here, key=lambda t: ab(*t)[0], reverse=not forward)
                if i > 0:
                    pts.append(xy(far_a if not forward else near_a, b))
                pts += ordered
                if i + 1 < len(row_taps):
                    pts.append(xy(far_a if forward else near_a, b))
            label = xy(far_a, row_bs[0])
            if len(row_taps) == 1:
                pts.append(label)
            elif label not in pts:
                pts.insert(len(row_taps[0]) + 1, label)
            pts = [(snap(x), snap(y)) for x, y in pts]
            taps = {(snap(x), snap(y)) for row in row_taps for x, y in row}
            paths[(ap[0], ap[1], apin["number"])] = WirePath(pts, taps, (snap(label[0]), snap(label[1])), d)
            # The region the wire, its parts and its label take.
            lab_a = far_a + len(net) * _CHAR + 2 * GRID
            corners = [xy(near_a, row_bs[0] - 2 * GRID), xy(lab_a, row_bs[-1] + 2 * GRID)]
            extra.append((min(c[0] for c in corners), min(c[1] for c in corners),
                          max(c[0] for c in corners), max(c[1] for c in corners)))
            cursor[key] = snap(lab_a + pad)
    right = max([bx1] + [b[2] for b in placed_boxes + extra])
    return hung, right, extra, paths


def placed_extent(p: Placed, pin_nets: dict[str, dict[str, str]], texts: dict | None = None):
    """A placed part's box on the sheet as drawn: its pin on a wire has no label."""
    nets = dict(pin_nets.get(p.ref, {}))
    if p.hook:
        nets.pop(p.hook[3], None)
    t = (texts or {}).get(p.ref) if len(p.pins) <= 2 else None
    x0, y0, x1, y1 = part_extent(p.pins, p.rot, nets, t)
    return (x0 + p.x, y0 + p.y, x1 + p.x, y1 + p.y)


def layout(
    blocks: list[Block],
    parts: dict[str, list[tuple[int, list[dict]]]],
    pin_nets: dict[str, dict[str, str]],
    texts: dict[str, tuple[str, str]] | None = None,
    *,
    origin: tuple[float, float] = (25.4, 25.4),
    max_width: float = 380.0,
    extra_height: float = 0.0,
) -> list[Placed]:
    """Place every block, and every part in it. Blocks fill rows, left to right.

    Inside a block the anchor stands at the left and its small parts fill
    columns to its right, no taller than the anchor (or 60 mm, for a small
    one), ordered so like sits with like: decoupling, then pull-ups, then
    series parts, then the rest. Every size is measured from what is drawn —
    pins, stubs, labels, power symbols, reference and value — because two
    things that overlap on a schematic are, to KiCad, connected.
    """
    texts = texts or {}
    units = {(r, u): pins for r, lst in parts.items() for u, pins in lst}
    pad = 4 * GRID
    title_h = 4 * GRID

    def order(ref: str) -> tuple:
        kinds = sorted(net_kind(n) for n in pin_nets.get(ref, {}).values())
        if set(kinds) == {"ground", "rail"}:
            rank = 0
        elif "rail" in kinds:
            rank = 1
        elif kinds.count("signal") == 2:
            rank = 2
        else:
            rank = 3
        return (rank, ref)

    # First every block on its own, to know how big each is; then the sheet.
    measured: list[tuple[Block, list[Placed], tuple[float, float, float, float]]] = []
    for block in blocks:
        local: list[Placed] = []
        ay, anchor_w = 0.0, 0.0
        smalls = []
        for ref, unit in block.members:
            pins = units[(ref, unit)]
            if ref == block.anchor:
                x0, y0, x1, y1 = part_extent(pins, 0, pin_nets.get(ref, {}))
                # On the grid from the start: parts hang off this anchor's pin
                # tips, and must land on exactly the same grid line.
                local.append(Placed(ref, unit, snap(-x0), snap(ay - y0), 0, pins))
                anchor_w = max(anchor_w, x1 - x0)
                ay += (y1 - y0) + pad
            else:
                smalls.append((ref, unit, pins))
        anchor_h = max(ay - pad, 0.0)
        smalls.sort(key=lambda t: order(t[0]))
        extra: list[tuple[float, float, float, float]] = []
        ends: dict[tuple[str, int, str], WirePath] = {}
        sx = anchor_w + (2 * pad if block.anchor else 0.0)
        if block.anchor:
            hung, right_end, extra, ends = _hang(local, smalls, pin_nets, texts, pad, order)
            smalls = [t for t in smalls if (t[0], t[1]) not in hung]
            sx = max(sx, right_end + pad)
        sy, col_w = 0.0, 0.0
        limit = max(anchor_h, 60.0)
        for ref, unit, pins in smalls:
            nets = pin_nets.get(ref, {})
            rot = _two_pin_rotation(pins, nets)
            x0, y0, x1, y1 = part_extent(pins, rot, nets, texts.get(ref))
            w, h = x1 - x0, y1 - y0
            if sy and sy + h > limit:
                sx += col_w + pad
                sy, col_w = 0.0, 0.0
            local.append(Placed(ref, unit, sx - x0, sy - y0, rot, pins))
            sy += h + pad
            col_w = max(col_w, w)
        # Measured from what was placed, not from the bookkeeping.
        boxes = [placed_extent(p, pin_nets, texts if p.ref != block.anchor else None) for p in local]
        boxes += extra
        box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes))
        measured.append((block, local, box, ends))

    sizes = [(max(b[2] - b[0], len(blk.title) * 2.54 * 0.85) + 2 * pad, b[3] - b[1] + 2 * pad + title_h)
             for blk, _, b, _ in measured]

    def pack(width: float) -> tuple[float, float]:
        """Rows no wider than ``width``: the area they take."""
        x = y = row = used_w = 0.0
        for w, h in sizes:
            if x and x + w > width:
                y += row + 2 * pad
                x = row = 0.0
            x += w + 2 * pad
            row = max(row, h)
            used_w = max(used_w, x)
        return used_w, y + row

    # The row width that lands on the smallest standard landscape sheet, by
    # trying each one the blocks allow. A fixed width stacked a board of four
    # wide blocks into one column on an A1 sheet that was mostly empty.
    candidates = sorted({sum(w + 2 * pad for w, _ in sizes[i:j])
                         for i in range(len(sizes)) for j in range(i + 1, len(sizes) + 1)})
    reserve = 2 * origin[0] + extra_height

    def sheet(width: float) -> tuple[int, float, float]:
        """Smallest sheet first; on one sheet, the fewest rows (it is landscape)."""
        w, h = pack(width)
        for rank, (pw, ph) in enumerate(SHEETS):
            if w + 2 * origin[0] <= pw and h + reserve <= ph:
                return (rank, h, w)
        return (len(SHEETS), h, w)

    width = min(candidates, key=sheet) if candidates else max_width

    placed: list[Placed] = []
    cx, cy, row_h = origin[0], origin[1], 0.0
    for (block, local, (minx, miny, maxx, maxy), ends), (bw, bh) in zip(measured, sizes):
        if cx > origin[0] and cx + bw > origin[0] + width:
            cx = origin[0]
            cy += row_h + 2 * pad
            row_h = 0.0
        offx, offy = snap(cx - minx + pad), snap(cy - miny + pad + title_h)
        for p in local:
            placed.append(Placed(p.ref, p.unit, snap(p.x + offx), snap(p.y + offy), p.rot, p.pins, p.hook))
        block.wire_paths = {k: v.moved(offx, offy) for k, v in ends.items()}
        block.x0, block.y0 = snap(cx), snap(cy)
        block.x1, block.y1 = snap(cx + bw), snap(cy + bh)
        cx = block.x1 + 2 * pad
        row_h = max(row_h, bh)
    return placed
