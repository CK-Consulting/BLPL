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
NETS = set({nets!r})
VIA_D, DRILL, TRACK_W, CLR, EDGE = {via_d!r}, {drill!r}, {track_w!r}, {clearance!r}, {edge!r}
mm = pcbnew.FromMM
b = pcbnew.LoadBoard(PCB)

outline = pcbnew.SHAPE_POLY_SET()
b.GetBoardPolygonOutlines(outline, True)
pads = list(b.GetPads())
vias = []
tracks = []  # fan-out tracks placed so far: ((x0, y0), (x1, y1))
# A hair over the rule: KiCad measures from exact copper, this from boxes and
# samples, and 0.195 mm against a 0.2 mm rule was the first result without it.
CLR = CLR + 0.02
keepouts = [z for z in b.Zones() if z.GetIsRuleArea() and z.GetDoNotAllowVias()]
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


def track_clear(a, b2, own):
    """The pad-to-via track, sampled every 0.05 mm: clear of every other pad,
    every via and every fan-out track already down. Checking only the via's
    spot let a track run across a neighbouring resistor's pad and short it."""
    n = max(2, int(math.hypot(b2[0] - a[0], b2[1] - a[1]) / 0.05) + 1)
    half = TRACK_W / 2.0
    for i in range(n + 1):
        x = a[0] + (b2[0] - a[0]) * i / n
        y = a[1] + (b2[1] - a[1]) * i / n
        for p in pads:
            if p.m_Uuid == own.m_Uuid:
                continue
            if inside(box_mm(p, half + CLR), x, y):
                return False
        for vx, vy in vias:
            if math.hypot(vx - x, vy - y) < r + half + CLR:
                return False
        for t in tracks:
            if seg_dist(x, y, *t) < TRACK_W + CLR:
                return False
    return True


def free(x, y, own):
    if not edge_clear(x, y):
        return False
    for p in pads:
        if p.m_Uuid == own.m_Uuid:
            continue
        if inside(box_mm(p, r + CLR), x, y):
            return False
    for vx, vy in vias:
        if math.hypot(vx - x, vy - y) < VIA_D + CLR:
            return False
    for t in tracks:
        if seg_dist(x, y, *t) < r + TRACK_W / 2.0 + CLR:
            return False
    for z in keepouts:
        if z.Outline().Contains(pcbnew.VECTOR2I(mm(x), mm(y))):
            return False
    return True


placed, failed = [], []
for pad in pads:
    net = pad.GetNetname()
    if net not in NETS or pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
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
    tracks.append(((cx, cy), spot))
    placed.append([ref, pad.GetNumber(), net])

pcbnew.SaveBoard(PCB, b)
print("FANOUT " + json.dumps({{"placed": placed, "failed": failed}}))
'''


def script(pcb: str, nets: list[str], *, via_d: float = VIA_DIAMETER, drill: float = VIA_DRILL,
           track_w: float = TRACK_WIDTH, clearance: float = CLEARANCE,
           edge: float = EDGE_CLEARANCE) -> str:
    """The pcbnew script that fans ``nets``' SMD pads out to their planes in ``pcb``."""
    return _SCRIPT.format(pcb=str(pcb), nets=sorted(nets), via_d=via_d, drill=drill,
                          track_w=track_w, clearance=clearance, edge=edge)


def parse(stdout: str) -> dict:
    """The script's answer: which pads got a via, and which had no room."""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("FANOUT "):
            return json.loads(line[len("FANOUT "):])
    return {"placed": [], "failed": [], "error": "the fan-out script gave no answer"}
