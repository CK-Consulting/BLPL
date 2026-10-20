"""Stage 6a with KiCad Routing Tools instead of Freerouting.

KiCad Routing Tools (https://github.com/drandyhaas/KiCadRoutingTools, MIT,
drandyhaas and contributors) routes a ``.kicad_pcb`` directly: a Rust A* grid
router behind Python CLIs, with plane pours and their vias, no Specctra round
trip. On verdacor-ear-tag Freerouting stalled at 53 open connections from its
first pass, while this, given the same placement and the project's own rules,
left 3 and passed KiCad's DRC.

What BLPL has to do around it, each learned on that board:

- **Re-save the board through pcbnew first.** Its parser reads only the quoted
  s-expression spelling (``(pad "1" ...)``, ``(layers "F.Cu")``). A vendor
  footprint written in the older unquoted one — the MAX-M10M on verdacor —
  was invisible to it: its pads were never routed and a track ran across them.
  pcbnew writes every footprint in the current form.
- **Keep the project file.** pcbnew's save rewrites the ``.kicad_pro`` beside
  the board, and that rewrite dropped every net class but a 0.2 mm Default;
  the router then also writes relaxed DRC floors into it. BLPL owns that file,
  so it is restored after each step, and Stage 7 grades the board against the
  project's real rules rather than the router's.
- **Route class by class.** The router routes one track width per call and does
  not read net-class widths, so every net came out at the Default width — the
  50 ohm RF feeds and the 0.75 mm power rails at 0.2 mm. Each non-Default class
  is routed first at its own width, then everything else at Default, with the
  power classes as ``--power-nets`` at theirs. Escalation is off, so it never
  routes below the project's floors to finish.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME_ENV = "KRT_HOME"
DEFAULT_HOME = Path("/opt/kicad-routing-tools")

#: Classes routed as power nets in the final pass: wide, tapered into pads.
POWER_CLASS = re.compile(r"power|pwr|bulk|supply", re.I)

_NET = re.compile(r'\(net (?:\d+ )?"([^"]+)"\)')


def find_home() -> Path | None:
    declared = os.environ.get(HOME_ENV)
    for candidate in ([Path(declared)] if declared else []) + [DEFAULT_HOME]:
        if (candidate / "py_router" / "route.py").is_file():
            return candidate
    return None


def _python_ok(python: str) -> bool:
    try:
        proc = subprocess.run([python, "-c", "import numpy, scipy, shapely"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def available() -> tuple[bool, str]:
    from . import autoroute

    home = find_home()
    missing = []
    if home is None:
        missing.append(f"KiCad Routing Tools (set {HOME_ENV})")
    elif not any((home / "rust_router").glob("grid_router*")):
        missing.append("its compiled router (rust_router/grid_router)")
    if not _python_ok(sys.executable):
        missing.append("numpy, scipy and shapely")
    if autoroute.find_pcbnew_python() is None:
        missing.append("a Python that can import pcbnew")
    if missing:
        return False, "routing with KiCad Routing Tools needs " + ", ".join(missing)
    return True, ""


def _board_rules(pro: Path) -> dict:
    """The project's board-wide DRC minimums, from .kicad_pro."""
    try:
        return ((json.loads(pro.read_text(encoding="utf-8")).get("board") or {})
                .get("design_settings") or {}).get("rules") or {}
    except (OSError, ValueError):
        return {}


def _classes(pro: Path) -> tuple[dict[str, dict], list[tuple[str, str]]]:
    """Net classes by name, and the (pattern, class) assignments, from .kicad_pro."""
    try:
        ns = json.loads(pro.read_text(encoding="utf-8")).get("net_settings") or {}
    except (OSError, ValueError):
        return {}, []
    classes = {c.get("name"): c for c in ns.get("classes") or [] if c.get("name")}
    patterns = [(p.get("pattern", ""), p.get("netclass", "")) for p in ns.get("netclass_patterns") or []]
    patterns += [(n, c) for n, c in (ns.get("netclass_assignments") or {}).items() if isinstance(c, str)]
    return classes, patterns


def nets_by_class(pcb: Path, pro: Path) -> tuple[dict[str, list[str]], dict[str, dict]]:
    """Every net on the board, grouped by its net class (Default when unassigned)."""
    classes, patterns = _classes(pro)
    nets = sorted({n for n in _NET.findall(pcb.read_text(encoding="utf-8", errors="replace")) if n})
    out: dict[str, list[str]] = {}
    for net in nets:
        cls = next((c for p, c in patterns if p == net or fnmatch.fnmatchcase(net, p)), "Default")
        out.setdefault(cls if cls in classes else "Default", []).append(net)
    return out, classes


def board_copper(pcb: Path) -> list[str]:
    """The board's copper layers, in stack order, from its layer table."""
    text = pcb.read_text(encoding="utf-8", errors="replace")
    found = re.findall(r'\(\s*\d+\s+"((?:F|B|In\d+)\.Cu)"', text)
    order = {"F.Cu": -1, "B.Cu": 10_000}
    return sorted(dict.fromkeys(found), key=lambda l: order.get(l, int(l[2:-3]) if l.startswith("In") else 0))


def _pat(net: str) -> str:
    """``net`` as an fnmatch pattern that matches that name and no other.

    route.py and route_diff.py read every --nets value, and --power-nets, as
    an fnmatch pattern, with no exact-name fallback: a bus net DATA[0] selected
    DATA0 and not itself, and its "!" protection guarded the wrong net (Codex,
    PR #59). ``*``, ``?`` and ``[`` become one-character classes. route_planes.py
    takes literal names and is never given these. Checked against KRT's own
    expand_net_patterns: each of DATA[0], A*B, Q?, !RESET and RESET selects and
    excludes itself alone. KRT still lets an unqualified name match a sheet
    path's leaf ("GND" also takes "/Sheet/GND"), by design; BLPL's schematics
    are flat, so its boards carry no such pair.
    """
    return "".join(f"[{c}]" if c in "*?[" else c for c in net)


def _glob(net: str) -> str:
    """A --nets selector for ``net``. A leading "!" is KRT's exclusion mark
    there, so an active-low net such as !RESET takes KRT's ``\\!`` escape and is
    routed, not excluded. --power-nets has no exclusion and takes ``_pat``."""
    out = _pat(net)
    return "\\" + out if out.startswith("!") else out


def _not(net: str) -> str:
    """The --nets selector that keeps ``net`` out of a pass.

    KRT reads "!X" as including the net "!X" when the board has one, so with
    an active-low !RESET beside RESET, "!RESET" routed !RESET instead of
    protecting RESET. A bracketed first character ("![R]ESET") names no net
    and still matches only RESET.
    """
    p = _pat(net)
    if p and p[0] not in "![":
        p = f"[{p[0]}]{p[1:]}"
    return "!" + p


def impedance_layers(hdm: dict | None) -> dict[str, list[str]]:
    """Each net class's declared impedance layers, from the HDM.

    Stage 5 resolves them, tags included, onto ``net_classes.<name>.impedance``;
    the .kicad_pro the router reads has no place for them.
    """
    out: dict[str, list[str]] = {}
    for name, rules in ((hdm or {}).get("net_classes") or {}).items():
        imp = (rules or {}).get("impedance")
        layers = imp.get("layers") if isinstance(imp, dict) else None
        if layers:
            out[str(name)] = [str(l) for l in layers]
    return out


def _diff_pairs(nets: list[str]) -> list[tuple[str, str]]:
    """The class's nets as (positive, negative) pairs, when every net has its
    partner; empty when the class is not a set of differential pairs.

    Partners come from Stage 4's own table, the one that put these nets in a
    differential class: a second, shorter list here knew _DP only with _DN,
    so the USB_DP/USB_DM spelling Stage 4 pairs went to the single-ended
    router (Codex, PR #59). Each net takes its first partner present in the
    class, so a class may mix conventions: USB_DP/USB_DN beside CLK+/CLK-.
    """
    from .stage4_synthesize_nets import _diff_pair_complements

    names = set(nets)
    pairs: list[tuple[str, str]] = []
    paired: set[str] = set()
    for n in sorted(names):
        if n in paired:
            continue
        partner = next((c for c in _diff_pair_complements(n)
                        if c in names and c not in paired and c != n), None)
        if partner is None:
            continue
        # Every negative spelling ends N, M or "-"; every positive one P or "+".
        pairs.append((n, partner) if partner.upper()[-1] in "NM-" else (partner, n))
        paired |= {n, partner}
    return sorted(pairs) if pairs and paired == names else []


def plan(pcb: Path, pro: Path, planes: dict[str, list[str]],
         copper_layers: list[str] | None = None, refused: list[dict] | None = None,
         band: bool | None = None, class_layers: dict[str, list[str]] | None = None) -> list[dict]:
    """The calls, in order: planes, each special class, then everything else.

    Pure, so the order and the rules each call carries can be tested without a
    router. Each step is {"script", "args"}; paths are filled in by ``route``.
    """
    groups, classes = nets_by_class(pcb, pro)
    default = classes.get("Default", {})
    copper_layers = copper_layers or board_copper(pcb)

    def rules(c: dict) -> list[str]:
        return ["--clearance", f"{c.get('clearance', default.get('clearance', 0.2)):g}",
                "--via-size", f"{c.get('via_diameter', default.get('via_diameter', 0.6)):g}",
                "--via-drill", f"{c.get('via_drill', default.get('via_drill', 0.3)):g}"]

    steps: list[dict] = []
    held = ["--escalation", "off", "--no-fix-drc-settings"]
    pad_gap = default.get("clearance", 0.2)
    # The board's own edge and hole rules, which the router otherwise takes
    # from its defaults: on verdacor it ran VDD_LR2021 0.2 mm from the edge of
    # a board that asks for 0.5, and DRC failed 40 times on it.
    board = _board_rules(pro)
    for key, flag in (("min_copper_edge_clearance", "--board-edge-clearance"),
                      ("min_hole_to_hole", "--hole-to-hole-clearance")):
        if isinstance(board.get(key), (int, float)) and board[key] > 0:
            held += [flag, f"{board[key]:g}"]
    # The edge band as a keepout only when it was drawn: KRT blocks every
    # closed shape on the keepout layer, so with no band the flag turned
    # whatever else was drawn there into a wall (Codex, PR #59). ``route``
    # says whether it drew one; planned alone, the edge rule decides.
    if band is None:
        edge = board.get("min_copper_edge_clearance")
        band = isinstance(edge, (int, float)) and edge > 0
    if band:
        held += ["--keepout", "--keepout-layer", BAND_LAYER]
    if planes:
        def class_of(net: str) -> tuple[str, dict]:
            name = next((c for c, ns in groups.items() if net in ns), "Default")
            return name, classes.get(name, default)

        # One (net, layer) pair per plane, positionally: a net may be a plane
        # on more than one layer (GND on In1.Cu and In2.Cu), and taking each
        # net's first layer poured the second plane nowhere. One call per net
        # class, so each plane's vias and clearance are its own class's, not
        # whichever class came first (Codex, PR #59).
        by_class: dict[str, list[tuple[str, str]]] = {}
        for net, ls in sorted(planes.items()):
            by_class.setdefault(class_of(net)[0], []).extend((net, l) for l in ls)
        gaps = []
        for name, pairs in sorted(by_class.items()):
            cls = classes.get(name, default)
            # Plane vias kept off their own pads. Left to it, the router
            # dropped a via into every plane pad it could: 46 untented vias in
            # pads on verdacor, which wick solder away in reflow unless the fab
            # fills and caps them.
            gap = cls.get("clearance", default.get("clearance", 0.2))
            gaps.append(gap)
            steps.append({"script": "route_planes.py", "args": [
                "--nets", *[n for n, _ in pairs], "--plane-layers", *[l for _, l in pairs],
                *rules(cls), "--same-net-pad-clearance", f"{gap:g}",
                # Stage 6 has already poured each plane, to the outline or to
                # the polygon boundaries.copper_zones gives. Without this the
                # router replaces that zone with its own pour, which can put a
                # plane where the project restricted it (Codex, PR #59); with
                # it the zone stays and only the stitching vias are added.
                "--skip-existing-zones"]})
        pad_gap = max(gaps)
    # Signals stay off the plane layers. Routed on them, the planes were cut
    # into islands (38 for GND on verdacor) and every signal over a cut lost
    # its reference: Stage 8's reference-plane and split findings. With no
    # planes every copper layer is named all the same: left out, route_diff.py
    # falls back to F.Cu and B.Cu and a 4-layer board's inner layers go
    # unused (Codex, PR #59).
    plane_layers = {l for ls in planes.values() for l in ls}
    signal_layers = [l for l in copper_layers if l not in plane_layers]
    # Vias off same-net pads on every board, planes or none: the default
    # lets a via into a pad, which wicks solder in reflow (Codex, PR #59).
    held = [*held, "--same-net-pad-clearance", f"{pad_gap:g}"]
    if not signal_layers and planes:
        # Every copper layer is a plane. Without --layers the router falls
        # back to all of them, routes signals through every pour and splits
        # it (Codex, PR #59): the signals are left open, with the reason. The
        # plane nets still get the route step that welds their pads into the
        # pour, each on its own plane layers only, so no net cuts another's.
        for net, ls in sorted(planes.items()):
            _, cls = class_of(net)
            steps.append({"script": "route.py", "args": [
                "--nets", _glob(net), "--track-width",
                f"{cls.get('track_width', default.get('track_width', 0.2)):g}",
                *rules(cls), *held, "--layers", *ls]})
        if refused is not None:
            rest = sorted({n for ns in groups.values() for n in ns} - set(planes))
            if rest:
                refused.append({"class": "*", "nets": rest, "reason": (
                    f"every copper layer ({', '.join(copper_layers)}) carries a plane, so signals "
                    "have no layer to route on that would not cut one")})
        return steps
    layer_args = ["--layers", *signal_layers]

    def layers_for(name: str) -> list[str] | None:
        """--layers for one class: its declared impedance layers when it has
        them, which the .kicad_pro does not carry. A width calculated for F.Cu
        is another impedance on an inner layer, so routing the class on every
        signal layer could lay it at the wrong one (Codex, PR #59). None when
        none of its layers is free to route on."""
        declared = (class_layers or {}).get(name)
        if not declared:
            return layer_args
        usable = [l for l in declared if l in signal_layers]
        if not usable:
            if refused is not None:
                refused.append({"class": name, "nets": sorted(groups[name]), "reason": (
                    f"its impedance is for {', '.join(declared)}, and none of those is a signal "
                    f"layer on this board ({', '.join(signal_layers)})")})
            return None
        return ["--layers", *usable]
    power: list[tuple[str, str, float]] = []
    # Nets an earlier pass routed, excluded from every later one: KRT rips up
    # and rebuilds a blocking net it was not asked to route, at the current
    # pass's width, so a 0.35 mm RF trace could come back at 0.2 mm (Codex,
    # PR #59). "!NET" protects it.
    done: list[str] = []
    for name in sorted(groups):
        if name == "Default":
            continue
        c = classes.get(name, {})
        # A power class by name, unless it is a set of pairs or carries its
        # own impedance layers: "LowPower_USB_90R" is a USB pair, and the
        # name alone sent it single-ended as power nets, without its pair
        # width and gap (Codex, PR #59).
        if (POWER_CLASS.search(name) and not _diff_pairs(groups[name])
                and not (class_layers or {}).get(name)):
            power += [(name, n, c.get("track_width", default.get("track_width", 0.2))) for n in groups[name]]
            continue
        protect = [_not(n) for n in done]
        on = layers_for(name)
        if on is None:
            continue
        if _diff_pairs(groups[name]):
            # A differential class goes to the differential router, with the
            # pair's own leg width and gap: route.py routes each half alone.
            gap = c.get("diff_pair_gap", c.get("clearance", 0.2))
            clear = c.get("clearance", default.get("clearance", 0.2))
            if gap < clear - 1e-9:
                # route_diff.py widens a gap below the clearance to the
                # clearance, so the pair would be laid at a different
                # impedance than its gap was calculated for, under a step that
                # still claimed the gap (Codex, PR #59). Left unrouted with
                # the reason, as Stage 8's ZC-012 reports it.
                if refused is not None:
                    refused.append({"class": name, "nets": sorted(groups[name]), "reason": (
                        f"diff_pair_gap {gap:g} mm is below the class clearance {clear:g} mm; "
                        "the router would widen it to the clearance and change the pair's impedance")})
                continue
            steps.append({"script": "route_diff.py", "args": [
                "--nets", *map(_glob, groups[name]), *protect,
                "--track-width", f"{c.get('diff_pair_width', c.get('track_width', 0.2)):g}",
                "--diff-pair-gap", f"{gap:g}",
                *rules(c), *held, *on]})
        else:
            steps.append({"script": "route.py", "args": [
                "--nets", *map(_glob, groups[name]), *protect, "--track-width", f"{c.get('track_width', 0.2):g}",
                *rules(c), *held, *on]})
        done += groups[name]
    def power_args(nets: list[tuple[str, str, float]]) -> list[str]:
        return ["--power-nets", *[_pat(n) for _, n, _ in nets], "--power-nets-widths", *[f"{w:g}" for _, _, w in nets]]

    # A power class whose vias differ from Default's gets a pass of its own.
    # Explicit --via-size/--via-drill apply to every selected net, so routed
    # with the Default nets a Power_Bulk VBAT asking for a 0.9/0.5 via got
    # Default's 0.6/0.3 (Codex, PR #59). Still routed as power nets, so the
    # neck-down into small pads is the same as in the shared pass.
    def via_of(c: dict) -> tuple:
        return (c.get("via_diameter", default.get("via_diameter", 0.6)),
                c.get("via_drill", default.get("via_drill", 0.3)))

    shared = [p for p in power if via_of(classes.get(p[0], {})) == via_of(default)]
    for name in sorted({p[0] for p in power} - {p[0] for p in shared}):
        own = [p for p in power if p[0] == name]
        steps.append({"script": "route.py", "args": [
            "--nets", *[_glob(n) for _, n, _ in own], *map(_not, done),
            "--track-width", f"{default.get('track_width', 0.2):g}",
            *rules(classes.get(name, {})), *held, *layer_args, *power_args(own)]})
        done += [n for _, n, _ in own]
    # The last pass names its nets: Default and the power classes, never a
    # special class. Given "*" it re-selected an RF net its own pass had not
    # finished and routed it at the Default width, which an explicit
    # --track-width applies to every selected net (Codex, PR #59).
    rest = sorted(set(groups.get("Default", [])) | {n for _, n, _ in shared})
    if rest:
        final = ["--nets", *map(_glob, rest), *map(_not, done), "--track-width", f"{default.get('track_width', 0.2):g}",
                 *rules(default), *held, *layer_args]
        if shared:
            final += power_args(shared)
        steps.append({"script": "route.py", "args": final})
    return steps


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _resave(python: str, pcb: Path) -> subprocess.CompletedProcess:
    from . import autoroute

    return autoroute._pcbnew(python, (
        "import pcbnew\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        f"pcbnew.SaveBoard({str(pcb)!r}, b)\n"
    ))


#: The User layer the edge band is drawn on, and the stroke width that marks
#: a shape as BLPL's own band so only those are removed afterwards.
BAND_LAYER = "Eco2.User"
_BAND_MARK_NM = 12_345


def _edge_band(python: str, pcb: Path, depth_mm: float, *, add: bool) -> subprocess.CompletedProcess:
    """Draw (or remove) a keepout band ``depth_mm`` wide inside every board edge.

    KRT holds the board-edge clearance in its main pass but not in all of
    them: on verdacor a power net's neck-down fallback laid 155 short segments
    0.2 mm from an angled edge of a board that asks for 0.5 mm. A band drawn
    on a User layer and handed to it as ``--keepout`` keeps every pass out,
    and is taken off the board again once routing is done.
    """
    from . import autoroute

    # Any band already on the board goes first, adding or not: one left by
    # a run that died before its cleanup must not be doubled by the next.
    remove = (
        "import pcbnew\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        f"lay = b.GetLayerID({BAND_LAYER!r})\n"
        # One pass, before anything is removed: KiCad 10's binding cannot
        # walk GetDrawings() again after a Remove (a TypeError, or a crash).
        "ours, others = [], 0\n"
        "for d in list(b.GetDrawings()):\n"
        "    if d.GetLayer() == lay:\n"
        # Only a shape has a width: text or a dimension on the layer is the
        # board's own and has none to read (Codex, PR #59).
        f"        if isinstance(d, pcbnew.PCB_SHAPE) and d.GetWidth() == {_BAND_MARK_NM}:\n"
        "            ours.append(d)\n"
        "        else:\n"
        "            others += 1\n"
        "for d in ours:\n"
        "    b.Remove(d)\n"
    )
    if not add:
        return autoroute._pcbnew(python, remove + f"pcbnew.SaveBoard({str(pcb)!r}, b)\n")
    # KRT keeps out of every closed shape on the layer, not only the band's:
    # a layer someone else drew on is not BLPL's to use (Codex, PR #59). It
    # exits 3, after saving the stale band's removal, and draws nothing.
    taken = (
        "if others:\n"
        f"    pcbnew.SaveBoard({str(pcb)!r}, b)\n"
        "    print(f'{others} drawing(s) already on " + BAND_LAYER + "')\n"
        "    raise SystemExit(3)\n"
    )
    return autoroute._pcbnew(python, remove + taken + (
        "poly = pcbnew.SHAPE_POLY_SET()\n"
        "b.GetBoardPolygonOutlines(poly, True)\n"
        f"d = pcbnew.FromMM({depth_mm!r})\n"
        # Each outline's band runs inward, and each cutout's (a hole in the
        # outline) outward into the board around it: banding the outlines
        # alone let routing close in on an internal cutout (Codex, PR #59).
        "rings = []\n"
        "for o in range(poly.OutlineCount()):\n"
        "    rings.append((poly.Outline(o), 1))\n"
        "    for h in range(poly.HoleCount(o)):\n"
        "        rings.append((poly.Hole(o, h), -1))\n"
        "for ring, side in rings:\n"
        "    pts = [ring.CPoint(i) for i in range(ring.PointCount())]\n"
        "    area = sum(pts[i].x * pts[(i + 1) % len(pts)].y - pts[(i + 1) % len(pts)].x * pts[i].y for i in range(len(pts)))\n"
        "    sign = (1 if area > 0 else -1) * side\n"
        "    for i in range(len(pts)):\n"
        "        a, c = pts[i], pts[(i + 1) % len(pts)]\n"
        "        dx, dy = c.x - a.x, c.y - a.y\n"
        "        ln = (dx * dx + dy * dy) ** 0.5\n"
        "        if ln == 0:\n"
        "            continue\n"
        "        nx, ny = -dy / ln * sign, dx / ln * sign\n"
        "        q = [a, c, pcbnew.VECTOR2I(int(c.x + nx * d), int(c.y + ny * d)), pcbnew.VECTOR2I(int(a.x + nx * d), int(a.y + ny * d))]\n"
        "        sh = pcbnew.PCB_SHAPE(b)\n"
        "        sh.SetShape(pcbnew.SHAPE_T_POLY)\n"
        "        sh.SetPolyPoints(q)\n"
        "        sh.SetLayer(lay)\n"
        f"        sh.SetWidth({_BAND_MARK_NM})\n"
        "        sh.SetFilled(True)\n"
        "        b.Add(sh)\n"
        f"pcbnew.SaveBoard({str(pcb)!r}, b)\n"
    ))


def _fill(python: str, pcb: Path) -> subprocess.CompletedProcess:
    from . import autoroute

    return autoroute._pcbnew(python, (
        "import pcbnew\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        "zones = b.Zones()\n"
        "if len(zones):\n"
        "    pcbnew.ZONE_FILLER(b).Fill(zones)\n"
        f"pcbnew.SaveBoard({str(pcb)!r}, b)\n"
    ))


def route(pcb: Path, *, work_dir: Path, timeout: int = 3600,
          class_layers: dict[str, list[str]] | None = None):
    """Route ``pcb`` in place with KiCad Routing Tools; the board's own measure."""
    from . import autoroute

    ok, why = available()
    if not ok:
        return autoroute.RouteResult(False, why, attempted=False, pcb=str(pcb))
    pcb = Path(pcb)
    pro = pcb.with_suffix(".kicad_pro")
    home = find_home()
    python = autoroute.find_pcbnew_python()
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    kept_pro = work / f"{pcb.stem}.kicad_pro.blpl"
    if pro.is_file():
        shutil.copyfile(pro, kept_pro)

    def restore_pro() -> None:
        if kept_pro.is_file():
            shutil.copyfile(kept_pro, pro)

    result = autoroute.RouteResult(False, "", pcb=str(pcb), extra={"router": "kicad-routing-tools"})
    log: list[str] = []
    resaved = _resave(python, pcb)
    restore_pro()
    if resaved.returncode != 0:
        result.reason = f"pcbnew could not re-save the board: {autoroute._msg(resaved)}"
        return result
    rules = _board_rules(pro)
    classes, _ = _classes(pro)
    edge = rules.get("min_copper_edge_clearance")
    band = isinstance(edge, (int, float)) and edge > 0
    if band:
        widest = max([c.get("track_width", 0.2) for c in classes.values()] or [0.2])
        banded = _edge_band(python, pcb, edge + widest / 2, add=True)
        restore_pro()
        if banded.returncode == 3:
            # The layer is the designer's: route without the band, and say so.
            band = False
            result.extra["edge_band"] = (f"not drawn: {BAND_LAYER} already carries the board's own "
                                         f"drawings ({((banded.stdout or '').strip().splitlines() or ['?'])[-1]}); the router "
                                         "holds the edge rule unaided")
        elif banded.returncode != 0:
            result.reason = f"pcbnew could not draw the edge band: {autoroute._msg(banded)}"
            return result
    refused: list[dict] = []
    steps = plan(pcb, pro, autoroute.plane_layers(pcb), refused=refused, band=band,
                 class_layers=class_layers)
    if refused:
        result.extra["refused"] = refused
    result.extra["steps"] = [" ".join([s["script"], *s["args"]]) for s in steps]
    for step in steps:
        script = str(home / "py_router" / step["script"])
        target = [str(pcb), str(pcb)] if step["script"] == "route_planes.py" else [str(pcb), "-O"]
        proc = _run([sys.executable, script, *target, *step["args"]], timeout)
        restore_pro()
        log.append(f"$ {step['script']} {' '.join(step['args'])}\n{(proc.stdout or '')[-4000:]}{(proc.stderr or '')[-2000:]}")
        if proc.returncode != 0:
            result.reason = f"{step['script']} exited {proc.returncode}: {autoroute._msg(proc)[-800:]}"
            result.log = "\n".join(log)[-20000:]
            # A failed route still takes its band off: left on, the board
            # carries BLPL's scaffolding as if it were design (Codex, PR #59).
            if band:
                unbanded = _edge_band(python, pcb, 0, add=False)
                restore_pro()
                if unbanded.returncode != 0:
                    result.reason += f"; and pcbnew could not remove the edge band: {autoroute._msg(unbanded)}"
            return result
    result.log = "\n".join(log)[-20000:]
    unbanded = _edge_band(python, pcb, 0, add=False) if band else subprocess.CompletedProcess([], 0, "", "")
    restore_pro()
    if unbanded.returncode != 0:
        result.reason = f"pcbnew could not remove the edge band: {autoroute._msg(unbanded)}"
        return result
    # The router leaves its zones unfilled, and an unfilled plane connects
    # nothing: measured as it stands, every plane pad would read as open.
    filled = _fill(python, pcb)
    restore_pro()
    if filled.returncode != 0:
        result.reason = f"pcbnew could not fill the zones: {autoroute._msg(filled)}"
        return result
    measured = autoroute._measure_board(python, pcb)
    if measured is None:
        result.reason = "the routed board could not be measured"
        return result
    result.unrouted = measured.get("unconnected")
    result.segments = measured.get("segments")
    # KRT finishes normally when it could route nothing and writes the board
    # back unchanged: no track with connections still open is a failed route,
    # as it is for Freerouting (Codex, PR #59). A board needing no track at
    # all is still complete.
    if not result.segments and result.unrouted:
        result.reason = (f"the router finished but the board has no track segments and "
                         f"{result.unrouted} connection(s) still open")
        return result
    result.ok = True
    return result
