"""Drop a via beside every surface-mount pad on a plane net, before routing.

Freerouting does not reach a plane: given ground on In1.Cu and VDD_3V3 on
In2.Cu as planes in its DSN, it placed not one via on either net, and
verdacor-ear-tag's ground and supply pads stayed as open as they were with no
planes at all. So this step does what a designer does by hand before
autorouting a board with planes: beside each SMD pad on a plane net it puts a
through via, joined to the pad by a short track, at the first spot clear of
every other pad, every via already placed, the board edge and any keepout that
forbids vias. The via and track are locked, so the router treats them as fixed
and routes the signals around them. A through-hole pad needs nothing: the
plane's fill joins it.

A pad with no clear spot is reported, not forced. It stays for the router as
any other connection, and for the triage that follows.

The work happens inside KiCad's Python (``pcbnew``), as the DSN export and SES
import do; this module only writes the script and reads its answer.
"""

from __future__ import annotations

import json

#: Via and track, in mm. A 0.6/0.3 mm via is the common default and within
#: every fab's standard rules; a 0.25 mm track carries a decoupling capacitor's
#: current over the millimetre or two it runs.
VIA_DIAMETER = 0.6
VIA_DRILL = 0.3
TRACK_WIDTH = 0.25
CLEARANCE = 0.2
EDGE_CLEARANCE = 0.5

_SCRIPT = r'''
import json, math, sys
import pcbnew

PCB = {pcb!r}
PLANES = {planes!r}  # net -> the copper layers its planes are on
NETS = set(PLANES)
VIA_D, DRILL, TRACK_W, CLR, EDGE = {via_d!r}, {drill!r}, {track_w!r}, {clearance!r}, {edge!r}
mm = pcbnew.FromMM
b = pcbnew.LoadBoard(PCB)

outline = pcbnew.SHAPE_POLY_SET()
b.GetBoardPolygonOutlines(outline, True)
pads = list(b.GetPads())

def pt(v):
    return (pcbnew.ToMM(v.x), pcbnew.ToMM(v.y))

# Seeded with what the board already carries: a rerun on a routed board has
# last run's locked fan-out and routing, and an empty start placed duplicates
# beside them or crossed them (Codex, PR #49).
vias = []          # (x, y) of every via, any net
via_nets = {{}}      # (x, y) -> net
tracks = []        # ((x0, y0), (x1, y1)) of every track, any net
for item in b.GetTracks():
    if item.GetClass() == "PCB_VIA":
        p = pt(item.GetPosition())
        vias.append(p)
        via_nets[p] = item.GetNetname()
    else:
        tracks.append((pt(item.GetStart()), pt(item.GetEnd()), item.GetNetname()))
# Each item's own rule, not one constant: the gap two items need is the larger
# of their net classes' clearances, so a class asking for more than the
# default gets it (Codex, PR #49). Not the board's biggest clearance either,
# which counts edge and hole rules (1.0 mm on verdacor-ear-tag) and left room
# for 7 vias of 38. Plus a hair: KiCad measures exact copper, this boxes and
# samples, and 0.195 mm against a 0.2 mm rule was the first result without it.
HAIR = 0.02


def own_clr(item):
    try:
        return max(CLR, pcbnew.ToMM(item.GetOwnClearance(pcbnew.F_Cu)))
    except Exception:
        return CLR


pad_clr = {{p.m_Uuid.AsString(): own_clr(p) for p in pads}}
via_clr = {{}}
for item in b.GetTracks():
    if item.GetClass() == "PCB_VIA":
        via_clr[pt(item.GetPosition())] = own_clr(item)
keepouts = [z for z in b.Zones() if z.GetIsRuleArea() and z.GetDoNotAllowVias()]
track_keepouts = [z for z in b.Zones() if z.GetIsRuleArea() and z.GetDoNotAllowTracks()]
r = VIA_D / 2.0


def box_mm(item, grow):
    bb = item.GetBoundingBox()
    return (pcbnew.ToMM(bb.GetLeft()) - grow, pcbnew.ToMM(bb.GetTop()) - grow,
            pcbnew.ToMM(bb.GetRight()) + grow, pcbnew.ToMM(bb.GetBottom()) + grow)


def inside(box, x, y):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def edge_clear(x, y):
    p = pcbnew.VECTOR2I(mm(x), mm(y))
    if outline.OutlineCount() and not outline.Contains(p):
        return False
    # Distance to the edge itself: the polygon's own SquaredDistance is zero
    # for any point inside it, which rejected every spot on the board.
    for i in range(outline.OutlineCount()):
        d = pcbnew.ToMM(int(math.sqrt(outline.COutline(i).SquaredDistance(p, True))))
        if d < EDGE + r:
            return False
    return True


def seg_dist(px, py, a, b2):
    (ax, ay), (bx, by) = a, b2
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def gap(mine, theirs):
    return max(mine, theirs) + HAIR


def track_clear(a, b2, own):
    """The pad-to-via track, sampled every 0.05 mm: clear of every other pad,
    every via and every fan-out track already down. Checking only the via's
    spot let a track run across a neighbouring resistor's pad and short it."""
    n = max(2, int(math.hypot(b2[0] - a[0], b2[1] - a[1]) / 0.05) + 1)
    half = TRACK_W / 2.0
    mine = own_clr(own)
    for i in range(n + 1):
        x = a[0] + (b2[0] - a[0]) * i / n
        y = a[1] + (b2[1] - a[1]) * i / n
        for p in pads:
            if p.m_Uuid == own.m_Uuid:
                continue
            if inside(box_mm(p, half + gap(mine, pad_clr.get(p.m_Uuid.AsString(), CLR))), x, y):
                return False
        for vx, vy in vias:
            if math.hypot(vx - x, vy - y) < r + half + gap(mine, via_clr.get((vx, vy), CLR)):
                return False
        for t in tracks:
            if seg_dist(x, y, t[0], t[1]) < TRACK_W + gap(mine, CLR):
                return False
        for z in track_keepouts:
            if z.Outline().Contains(pcbnew.VECTOR2I(mm(x), mm(y))):
                return False
    return True


def free(x, y, own):
    if not edge_clear(x, y):
        return False
    mine = own_clr(own)
    for p in pads:
        if p.m_Uuid == own.m_Uuid:
            continue
        if inside(box_mm(p, r + gap(mine, pad_clr.get(p.m_Uuid.AsString(), CLR))), x, y):
            return False
    for vx, vy in vias:
        if math.hypot(vx - x, vy - y) < VIA_D + gap(mine, via_clr.get((vx, vy), CLR)):
            return False
    for t in tracks:
        if seg_dist(x, y, t[0], t[1]) < r + TRACK_W / 2.0 + gap(mine, CLR):
            return False
    for z in keepouts:
        if z.Outline().Contains(pcbnew.VECTOR2I(mm(x), mm(y))):
            return False
    return True


def already_down(pad, net):
    """A track from this pad to a via on its own net: last run's fan-out."""
    here = pt(pad.GetPosition())
    for a, b2, tnet in tracks:
        if tnet != net:
            continue
        for end, other in ((a, b2), (b2, a)):
            if math.hypot(end[0] - here[0], end[1] - here[1]) < 0.01 and via_nets.get(other) == net:
                return True
    return False


placed, failed, kept = [], [], []
for pad in pads:
    net = pad.GetNetname()
    if net not in NETS or pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
        continue
    # A pad on its plane's own layer is joined by the fill: an F.Cu plane
    # needs no via for an F.Cu pad, a B.Cu plane does.
    if any(pad.IsOnLayer(b.GetLayerID(name)) for name in PLANES[net]):
        continue
    if already_down(pad, net):
        kept.append([pad.GetParentFootprint().GetReference(), pad.GetNumber(), net])
        continue
    cx, cy = pcbnew.ToMM(pad.GetPosition().x), pcbnew.ToMM(pad.GetPosition().y)
    x0, y0, x1, y1 = box_mm(pad, 0.0)
    hw, hh = (x1 - x0) / 2.0, (y1 - y0) / 2.0
    spot = None
    for extra in (0.0, 0.3, 0.6, 1.0, 1.5):
        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (-1, 1), (1, -1), (-1, -1)):
            reach_x = (hw + r + CLR + extra) * dx
            reach_y = (hh + r + CLR + extra) * dy
            x, y = cx + reach_x, cy + reach_y
            if free(x, y, pad) and track_clear((cx, cy), (x, y), pad):
                spot = (x, y)
                break
        if spot:
            break
    ref = pad.GetParentFootprint().GetReference()
    if spot is None:
        failed.append([ref, pad.GetNumber(), net])
        continue
    layer = pcbnew.F_Cu if pad.IsOnLayer(pcbnew.F_Cu) else pcbnew.B_Cu
    v = pcbnew.PCB_VIA(b)
    v.SetPosition(pcbnew.VECTOR2I(mm(spot[0]), mm(spot[1])))
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetWidth(mm(VIA_D))
    v.SetDrill(mm(DRILL))
    v.SetNet(pad.GetNet())
    v.SetLocked(True)
    b.Add(v)
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(pad.GetPosition())
    t.SetEnd(v.GetPosition())
    t.SetWidth(mm(TRACK_W))
    t.SetLayer(layer)
    t.SetNet(pad.GetNet())
    t.SetLocked(True)
    b.Add(t)
    vias.append(spot)
    via_nets[spot] = net
    via_clr[spot] = own_clr(pad)
    tracks.append(((cx, cy), spot, net))
    placed.append([ref, pad.GetNumber(), net])

pcbnew.SaveBoard(PCB, b)
print("FANOUT " + json.dumps({{"placed": placed, "failed": failed, "kept": kept}}))
'''


def script(pcb: str, planes: "dict[str, list[str]] | list[str]", *, via_d: float = VIA_DIAMETER,
           drill: float = VIA_DRILL, track_w: float = TRACK_WIDTH, clearance: float = CLEARANCE,
           edge: float = EDGE_CLEARANCE) -> str:
    """The pcbnew script that fans each plane net's SMD pads out to its plane.

    ``planes`` is net -> the copper layers its planes are on (a bare list of
    nets means inner-layer planes, which every SMD pad needs a via to reach).
    """
    if not isinstance(planes, dict):
        planes = {n: ["In1.Cu"] for n in planes}
    return _SCRIPT.format(pcb=str(pcb), planes={n: sorted(v) for n, v in sorted(planes.items())},
                          via_d=via_d, drill=drill, track_w=track_w, clearance=clearance, edge=edge)


def parse(stdout: str) -> dict:
    """The script's answer: which pads got a via, and which had no room."""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("FANOUT "):
            return json.loads(line[len("FANOUT "):])
    return {"placed": [], "failed": [], "error": "the fan-out script gave no answer"}
