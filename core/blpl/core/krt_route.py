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


def plan(pcb: Path, pro: Path, planes: dict[str, list[str]],
         copper_layers: list[str] | None = None) -> list[dict]:
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
    held = ["--escalation", "off", "--no-fix-drc-settings", "--keepout", "--keepout-layer", BAND_LAYER]
    # The board's own edge and hole rules, which the router otherwise takes
    # from its defaults: on verdacor it ran VDD_LR2021 0.2 mm from the edge of
    # a board that asks for 0.5, and DRC failed 40 times on it.
    board = _board_rules(pro)
    for key, flag in (("min_copper_edge_clearance", "--board-edge-clearance"),
                      ("min_hole_to_hole", "--hole-to-hole-clearance")):
        if isinstance(board.get(key), (int, float)) and board[key] > 0:
            held += [flag, f"{board[key]:g}"]
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
                *rules(cls), "--same-net-pad-clearance", f"{gap:g}"]})
        # The routing passes too: the power-net taps in the last pass dropped
        # their vias into pads just as the plane step did.
        held = [*held, "--same-net-pad-clearance", f"{max(gaps):g}"]
        # Signals stay off the plane layers. Routed on them, the planes were
        # cut into islands (38 for GND on verdacor) and every signal over a
        # cut lost its reference: Stage 8's reference-plane and split findings.
        plane_layers = {l for ls in planes.values() for l in ls}
        signal_layers = [l for l in copper_layers if l not in plane_layers]
        if signal_layers:
            held = [*held, "--layers", *signal_layers]
    power: list[tuple[str, float]] = []
    for name in sorted(groups):
        if name == "Default":
            continue
        c = classes.get(name, {})
        if POWER_CLASS.search(name):
            power += [(n, c.get("track_width", default.get("track_width", 0.2))) for n in groups[name]]
            continue
        steps.append({"script": "route.py", "args": [
            "--nets", *groups[name], "--track-width", f"{c.get('track_width', 0.2):g}", *rules(c), *held]})
    # The last pass names its nets: Default and the power classes, never a
    # special class. Given "*" it re-selected an RF net its own pass had not
    # finished and routed it at the Default width, which an explicit
    # --track-width applies to every selected net (Codex, PR #59).
    rest = sorted(set(groups.get("Default", [])) | {n for n, _ in power})
    if rest:
        final = ["--nets", *rest, "--track-width", f"{default.get('track_width', 0.2):g}",
                 *rules(default), *held]
        if power:
            final += ["--power-nets", *[n for n, _ in power],
                      "--power-nets-widths", *[f"{w:g}" for _, w in power]]
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

    if not add:
        return autoroute._pcbnew(python, (
            "import pcbnew\n"
            f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
            f"lay = b.GetLayerID({BAND_LAYER!r})\n"
            "for d in list(b.GetDrawings()):\n"
            f"    if d.GetLayer() == lay and d.GetWidth() == {_BAND_MARK_NM}:\n"
            "        b.Remove(d)\n"
            f"pcbnew.SaveBoard({str(pcb)!r}, b)\n"
        ))
    return autoroute._pcbnew(python, (
        "import pcbnew\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        f"lay = b.GetLayerID({BAND_LAYER!r})\n"
        "poly = pcbnew.SHAPE_POLY_SET()\n"
        "b.GetBoardPolygonOutlines(poly, True)\n"
        f"d = pcbnew.FromMM({depth_mm!r})\n"
        "for o in range(poly.OutlineCount()):\n"
        "    ring = poly.Outline(o)\n"
        "    pts = [ring.CPoint(i) for i in range(ring.PointCount())]\n"
        "    area = sum(pts[i].x * pts[(i + 1) % len(pts)].y - pts[(i + 1) % len(pts)].x * pts[i].y for i in range(len(pts)))\n"
        "    sign = 1 if area > 0 else -1\n"
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


def route(pcb: Path, *, work_dir: Path, timeout: int = 3600):
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
        if banded.returncode != 0:
            result.reason = f"pcbnew could not draw the edge band: {autoroute._msg(banded)}"
            return result
    steps = plan(pcb, pro, autoroute.plane_layers(pcb))
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
    result.ok = True
    result.unrouted = measured.get("unconnected")
    result.segments = measured.get("segments")
    return result
