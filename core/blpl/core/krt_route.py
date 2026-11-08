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


def _startup_problem(home: Path) -> str | None:
    """What KRT's own startup check says is wrong with this install, or None.

    A file named grid_router* and importable numpy said nothing about whether
    the router would start: a stale or wrong-ABI binary, or a dependency below
    KRT's floor, passed here and KRT then exited on its first call, with
    Freerouting usable and never chosen (Codex, PR #59). startup_checks.py is
    the gate every KRT script runs; run on its own it exits 1 with the reason
    (checked: a version mismatch and a missing module both do).
    """
    try:
        proc = subprocess.run([sys.executable, str(home / "py_router" / "startup_checks.py")],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if proc.returncode == 0:
        return None
    lines = [l.strip() for l in ((proc.stdout or "") + (proc.stderr or "")).splitlines() if l.strip()]
    return lines[0] if lines else f"startup_checks.py exited {proc.returncode}"


def available() -> tuple[bool, str]:
    from . import autoroute

    home = find_home()
    missing = []
    if home is None:
        missing.append(f"KiCad Routing Tools (set {HOME_ENV})")
    elif not any((home / "rust_router").glob("grid_router*")):
        missing.append("its compiled router (rust_router/grid_router)")
    elif (problem := _startup_problem(home)) is not None:
        missing.append(f"an install that passes its own startup check ({problem})")
    if not _python_ok(sys.executable):
        missing.append("numpy, scipy and shapely")
    if autoroute.find_pcbnew_python() is None:
        missing.append("a Python that can import pcbnew")
    if missing:
        return False, "routing with KiCad Routing Tools needs " + ", ".join(missing)
    return True, ""


def _board_rules(pro: Path) -> dict:
    """The project's board-wide DRC minimums, from .kicad_pro.

    KiCad's defaults stand in for any the file leaves out, because they are
    what its DRC applies then. The router's own default for the edge is 0 mm:
    on a board whose .kicad_pro named no edge rule it routed to the edge and
    DRC failed on every track near it.
    """
    from ..emitter.pro import BOARD_RULE_DEFAULTS

    try:
        rules = ((json.loads(pro.read_text(encoding="utf-8")).get("board") or {})
                 .get("design_settings") or {}).get("rules") or {}
    except (OSError, ValueError):
        rules = {}
    return {**BOARD_RULE_DEFAULTS, **rules}


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


#: project.yaml's routing.search keys, and the flag each becomes on every
#: route.py and route_diff.py call. All of them steer the search and none
#: sets a size, a clearance or a layer, so no setting here can lay copper
#: below the project's own rules. The router's answer turns on them: on the
#: ear tag a hole added at one corner left a net open at the other, and
#: until these were settings the only lever was moving parts until it routed.
SEARCH_FLAGS = {
    "ordering": "--ordering",
    "direction": "--direction",
    "mps_reverse_rounds": "--mps-reverse-rounds",
    "max_ripup": "--max-ripup",
    "ripup_blocker_select": "--ripup-blocker-select",
    "max_iterations": "--max-iterations",
    "heuristic_weight": "--heuristic-weight",
}
_SEARCH_SCRIPTS = ("route.py", "route_diff.py")


def search_args(search: dict | None) -> list[str]:
    """The router flags for one routing.search mapping. A key the router does
    not take is an error, not a skip: a misspelt setting quietly ignored would
    route the board the default way while the project says otherwise."""
    unknown = sorted(set(search or {}) - set(SEARCH_FLAGS))
    if unknown:
        raise ValueError(f"routing.search: unknown setting(s) {', '.join(unknown)}; "
                         f"known: {', '.join(SEARCH_FLAGS)}")
    out: list[str] = []
    for key, flag in SEARCH_FLAGS.items():
        value = (search or {}).get(key)
        # A switch is its flag alone; false is the router's own default.
        if value is None or value is False:
            continue
        out += [flag] if value is True else [flag, f"{value:g}" if isinstance(value, float) else str(value)]
    return out


FANOUT = "bga_fanout.py"
FANOUT_ESCAPES = ("dogbone", "channel", "underpad", "auto")


def check_fanout(fanout) -> list[dict] | None:
    """routing.fanout, checked: a list of {ref, escape?} mappings, or None
    when the project does not set it.

    Stage 6a reads project.yaml without the schema, so a malformed entry
    reaches here as it was written. Refused before the board is touched, it
    is a reason in the report; found mid-route, it was a KeyError with the
    edge band still on the board and no report at all (Codex, PR #104).
    """
    if fanout is None:
        return None  # not set: the default, every U part (see _fanout_steps)
    if not isinstance(fanout, list):
        # ValueError, as every settings refusal here: route() turns those
        # into a failed result rather than a crash.
        raise ValueError("routing.fanout: a list of {ref, escape} entries")  # noqa: TRY004
    for i, entry in enumerate(fanout):
        if not isinstance(entry, dict) or not isinstance(entry.get("ref"), str) or not entry["ref"]:
            raise ValueError(f"routing.fanout[{i}]: each entry needs a ref, e.g. {{ref: U1}}")
        unknown = sorted(set(entry) - {"ref", "escape"})
        if unknown:
            raise ValueError(f"routing.fanout[{i}]: unknown setting(s) {', '.join(unknown)}; known: ref, escape")
        if entry.get("escape", "dogbone") not in FANOUT_ESCAPES:
            raise ValueError(f"routing.fanout[{i}]: escape {entry['escape']!r}; one of {', '.join(FANOUT_ESCAPES)}")
    return fanout

_REF = re.compile(r'\(property "Reference" "([^"]+)"')


def footprint_pads(pcb_text: str) -> dict[str, list[tuple[float, float, str, float, float, bool]]]:
    """Each footprint's pads as (x, y, net, width, height, smd), in the
    footprint's own frame, so a part's grid reads the same at any rotation.
    ``smd`` is a surface pad with copper; a paste-only aperture is left out."""
    from ..emitter import sexpr

    tree = sexpr.parse(pcb_text)
    top = tree if sexpr.head(tree) == "kicad_pcb" else tree[0]
    out: dict[str, list[tuple[float, float, str, float, float, bool]]] = {}
    for fp in sexpr.find_all(top, "footprint"):
        ref = next((sexpr.unquote(c[2]) for c in fp if isinstance(c, list) and sexpr.head(c) == "property"
                    and len(c) > 2 and sexpr.unquote(c[1]) == "Reference"), None)
        if ref is None:
            continue
        pads = []
        for pad in sexpr.find_all(fp, "pad"):
            at = sexpr.find(pad, "at")
            if at is None or len(at) < 3:
                continue
            layers = sexpr.find(pad, "layers")
            names = [sexpr.unquote(x) for x in (layers[1:] if layers is not None else [])]
            if names and not any(n.endswith(".Cu") for n in names):
                continue  # a paste or mask aperture, not a pad
            size = sexpr.find(pad, "size")
            w, h = (float(size[1]), float(size[2])) if size is not None and len(size) > 2 else (0.0, 0.0)
            net = sexpr.find(pad, "net")
            name = sexpr.unquote(net[-1]) if net is not None and len(net) > 1 else ""
            smd = len(pad) > 2 and pad[2] == "smd"
            pads.append((float(at[1]), float(at[2]), name, w, h, smd))
        out[ref] = pads
    return out


def is_grid(pads: list[tuple[float, float, str, float, float, bool]]) -> bool:
    """Whether a part's pads fill a grid: some pad is surrounded by others.

    A BGA or an LGA module has pads with neighbours on every side, which is
    what the router cannot leave. A part whose pads all sit on its outline (a
    SOIC, a module's castellations, a 2 x 3 WCSP) is reached from outside
    already, and fanning it out only spends a via on every pin: on
    verdacor-ear-tag-bm20, fanning out every U part took the board from 6
    open connections to 15, with a short.

    So a pad counts as inside only with a pad on each of its four sides at
    about the part's own pitch, the way a ball in a BGA has. A QFN's exposed
    pad, its thermal vias and any paste split of it lie inside the ring too,
    but the ring is far from them, not a pitch away (Codex, PR #106; checked
    against KiCad's Package_DFN_QFN and Package_BGA libraries).
    """
    smd = [p for p in pads if p[5]]
    if len(smd) < 9:
        return False
    import math

    def dist(a, b) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1])

    nearest = sorted(min(dist(p, q) for q in smd if q is not p) for p in smd)
    pitch = nearest[len(nearest) // 2]
    if pitch <= 0:
        return False
    reach = 1.5 * pitch
    sizes = sorted(max(p[3], p[4]) for p in smd)
    typical = sizes[len(sizes) // 2]

    def under_bigger(p) -> bool:
        return any(q is not p and max(q[3], q[4]) > max(p[3], p[4])
                   and abs(q[0] - p[0]) < q[3] / 2 and abs(q[1] - p[1]) < q[4] / 2 for q in pads)

    def surrounded(p) -> bool:
        if max(p[3], p[4]) > 1.5 * typical or under_bigger(p):
            return False  # an exposed pad, or a piece of one, not a ball
        sides = set()
        for q in smd:
            if q is p:
                continue
            dx, dy = q[0] - p[0], q[1] - p[1]
            if abs(dx) <= reach and abs(dy) <= 0.5 * pitch and abs(dx) > 0.5 * pitch:
                sides.add("+x" if dx > 0 else "-x")
            if abs(dy) <= reach and abs(dx) <= 0.5 * pitch and abs(dy) > 0.5 * pitch:
                sides.add("+y" if dy > 0 else "-y")
        return len(sides) == 4

    return any(surrounded(p) for p in smd)


def plan(pcb: Path, pro: Path, planes: dict[str, list[str]],
         copper_layers: list[str] | None = None, refused: list[dict] | None = None,
         band: bool | None = None, class_layers: dict[str, list[str]] | None = None,
         search: dict | None = None, fanout: list[dict] | None = None) -> list[dict]:
    """The calls, in order: fan-outs, planes, each special class, then the rest.

    Pure, so the order and the rules each call carries can be tested without a
    router. Each step is {"script", "args"}; paths are filled in by ``route``.
    ``search`` (routing.search) goes on every track-laying call; the plane
    calls only stitch and pour, and take none of it. ``fanout``
    (routing.fanout) comes first: the router reads the escapes as copper
    already laid and routes on from their vias.
    """
    extra = search_args(search)
    steps = _fanout_steps(pcb, pro, planes, copper_layers, refused, fanout, class_layers)
    steps += _plan(pcb, pro, planes, copper_layers, refused, band, class_layers)
    for step in steps:
        if step["script"] in _SEARCH_SCRIPTS:
            step["args"] = [*step["args"], *extra]
    return steps


def _fanout_steps(pcb: Path, pro: Path, planes: dict[str, list[str]],
                  copper_layers: list[str] | None, refused: list[dict] | None,
                  fanout: list[dict] | None,
                  class_layers: dict[str, list[str]] | None = None) -> list[dict]:
    """One bga_fanout.py call per routing.fanout entry, at the Default class.

    The router alone could not leave a dense grid of pads. On the BM20BC's
    0.9 mm LGA (verdacor-ear-tag-bm20) it left 18 of the module's
    connections open; fanned out first, between the pads, the whole board
    went to 10. Default's sizes because an escape is a short stub between
    pads whatever the net: a power class's width is what does not fit there.
    The escapes stay off the plane layers, as every signal does.
    """
    fanout = check_fanout(fanout)
    text = Path(pcb).read_text(encoding="utf-8")
    on_board = set(_REF.findall(text))
    pads = footprint_pads(text)
    if fanout is None:
        # Not set: every IC (U1, U2, U_MCU) whose pads fill a grid, the part
        # the router cannot leave and nobody thought to name. Parts with
        # their pads on the outline are left alone (is_grid). An empty list
        # turns it off. Each part is optional here: one the fan-out cannot
        # handle is reported, and the route goes on.
        fanout = [{"ref": r, "optional": True} for r in sorted(on_board)
                  if r.startswith("U") and is_grid(pads.get(r, []))]
    if not fanout:
        return []
    groups, classes = nets_by_class(pcb, pro)
    default = classes.get("Default", {})
    # Never an escape on an impedance-controlled net: a stub and a via on a
    # 50 ohm feed is the discontinuity the class exists to avoid, and on
    # verdacor-ear-tag-bm20 both of U2's RF feeds were left open by it.
    controlled = {n for name in (class_layers or {}) for n in groups.get(name, [])}
    plane_layers = {l for ls in planes.values() for l in ls}
    signal_layers = [l for l in (copper_layers or board_copper(pcb)) if l not in plane_layers]
    steps = []
    for entry in fanout:
        ref = entry["ref"]
        if ref not in on_board:
            # A misspelt or removed part: said, and the rest still routes.
            if refused is not None:
                refused.append({"fanout": ref, "reason": "no footprint with this reference on the board"})
            continue
        nets = sorted({p[2] for p in pads.get(ref, []) if p[2]})
        only: list[str] = []
        if controlled & set(nets):
            keep = [n for n in nets if n not in controlled]
            if not keep:
                continue
            only = ["--nets", *map(_glob, keep)]
        steps.append({"script": FANOUT, "optional": bool(entry.get("optional")), "args": [
            "--component", ref,
            "--escape-method", entry.get("escape", "dogbone"),
            "--track-width", f"{default.get('track_width', 0.2):g}",
            "--clearance", f"{default.get('clearance', 0.2):g}",
            "--via-size", f"{default.get('via_diameter', 0.6):g}",
            "--via-drill", f"{default.get('via_drill', 0.3):g}",
            "--layers", *signal_layers, *only,
            # As every other call: the project's rules, never relaxed.
            "--escalation", "off", "--no-fix-drc-settings"]})
    return steps


def _plan(pcb: Path, pro: Path, planes: dict[str, list[str]],
          copper_layers: list[str] | None, refused: list[dict] | None,
          band: bool | None, class_layers: dict[str, list[str]] | None) -> list[dict]:
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
    controlled_last = None
    # The impedance-controlled classes first: once they are down, the via
    # bands beside them (VIA_BANDS) keep every later pass's vias out of the
    # plane they are measured against.
    for name in sorted(groups, key=lambda n: (n not in (class_layers or {}), n)):
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
        if name in (class_layers or {}):
            controlled_last = len(steps)
    if controlled_last is not None:
        steps.insert(controlled_last, {"script": VIA_BANDS, "args": sorted(
            n for name in (class_layers or {}) for n in groups.get(name, []))})
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


def _edge_band(python: str, pcb: Path, depth_mm: float, *, add: bool,
               hole_ring_mm: float = 0.0) -> subprocess.CompletedProcess:
    """Draw (or remove) a keepout band ``depth_mm`` wide inside every board edge.

    KRT holds the board-edge clearance in its main pass but not in all of
    them: on verdacor a power net's neck-down fallback laid 155 short segments
    0.2 mm from an angled edge of a board that asks for 0.5 mm. A band drawn
    on a User layer and handed to it as ``--keepout`` keeps every pass out,
    and is taken off the board again once routing is done.

    ``hole_ring_mm`` also rings every NPTH hole, that far beyond its edge.
    The router has no copper-to-hole rule: it held a track's ordinary
    clearance to J_DBG's alignment holes, 0.21 mm where DRC asks 0.25.
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
        # A ring around each NPTH as a 24-sided polygon, the shape KRT is
        # known to block; an oval hole takes its longer axis.
        f"r_extra = pcbnew.FromMM({hole_ring_mm!r})\n"
        "import math\n"
        "if r_extra > 0:\n"
        "    for fp in b.GetFootprints():\n"
        "        for pad in fp.Pads():\n"
        "            if pad.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH:\n"
        "                continue\n"
        "            ds = pad.GetDrillSize()\n"
        "            r = max(ds.x, ds.y) / 2 + r_extra\n"
        "            c = pad.GetPosition()\n"
        "            q = [pcbnew.VECTOR2I(int(c.x + r * math.cos(2 * math.pi * k / 24)), int(c.y + r * math.sin(2 * math.pi * k / 24))) for k in range(24)]\n"
        "            sh = pcbnew.PCB_SHAPE(b)\n"
        "            sh.SetShape(pcbnew.SHAPE_T_POLY)\n"
        "            sh.SetPolyPoints(q)\n"
        "            sh.SetLayer(lay)\n"
        f"            sh.SetWidth({_BAND_MARK_NM})\n"
        "            sh.SetFilled(True)\n"
        "            b.Add(sh)\n"
        f"pcbnew.SaveBoard({str(pcb)!r}, b)\n"
    ))


#: A step in ``plan`` that is not a router call: draw the via bands.
VIA_BANDS = "@via-bands"
#: The rule areas' name, by which they are found and taken off again.
VIA_BAND_NAME = "BLPL via band"


def via_bands(pcb_text: str, nets: list[str], *, via_mm: float, clearance_mm: float) -> list[list[tuple[float, float]]]:
    """Rectangles beside every measured sample of the controlled nets, inside
    which no other net's via may go.

    The release holds a controlled trace to filled plane under it out to
    ``impedance._PLANE_MARGIN_W`` widths past each edge, on its reference
    layer. Another net's via through that plane cuts a hole its radius plus
    the plane's clearance wide, and the router, routing the rest afterwards,
    put them there: on the ear tag VDD_LR2021's vias straddled RF_LRLF and
    left both of its edges over no plane. The band is that margin plus that
    hole, on the samples the release measures: the breakouts by the pads,
    which it does not measure, stay free for a dense part's fan-out.
    """
    from blpl.emitter import sexpr

    from . import impedance

    board = sexpr.parse(pcb_text)
    _, _, lines, arcs = impedance._routed(board)
    pads = impedance._pads(board)
    out = []
    for net in nets:
        for _lay, pt, nrm, w, step, _end in impedance._measured(net, lines, arcs, pads.get(net)):
            half_w = w / 2 + impedance._PLANE_MARGIN_W * w + via_mm / 2 + clearance_mm
            half_l = step / 2 + 0.05  # overlapping, so a run has no gaps
            tx, ty = -nrm[1], nrm[0]
            out.append([(pt[0] + sx * tx * half_l + sy * nrm[0] * half_w,
                         pt[1] + sx * ty * half_l + sy * nrm[1] * half_w)
                        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
    return out


def _via_band_areas(python: str, pcb: Path, rects: list | None) -> subprocess.CompletedProcess:
    """Write ``rects`` as rule areas forbidding vias on every copper layer, or
    with None take every one this module drew off the board again.

    The rectangles go to the script in a file beside the board, not in its
    source: a sample every 0.25 mm makes 2,400 of them for 600 mm of
    controlled trace, and a script carrying them all was more than Linux
    takes as one ``python -c`` argument (Codex, PR #78)."""
    import json

    from . import autoroute

    geometry = pcb.with_name(pcb.name + ".via-bands.json")
    code = (
        "import json, pcbnew\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        f"for z in [z for z in b.Zones() if z.GetIsRuleArea() and z.GetZoneName() == {VIA_BAND_NAME!r}]:\n"
        "    b.Remove(z)\n"
    )
    if rects:
        geometry.write_text(json.dumps([[(round(x, 4), round(y, 4)) for x, y in r] for r in rects]),
                            encoding="utf-8")
        code += (
            f"for r in json.load(open({str(geometry)!r}, encoding='utf-8')):\n"
            "    z = pcbnew.ZONE(b)\n"
            "    z.SetIsRuleArea(True)\n"
            f"    z.SetZoneName({VIA_BAND_NAME!r})\n"
            "    z.SetDoNotAllowVias(True)\n"
            "    z.SetDoNotAllowTracks(False)\n"
            "    z.SetDoNotAllowZoneFills(False)\n"
            "    z.SetDoNotAllowPads(False)\n"
            "    z.SetDoNotAllowFootprints(False)\n"
            "    z.SetLayerSet(pcbnew.LSET.AllCuMask())\n"
            "    ol = z.Outline()\n"
            "    ol.NewOutline()\n"
            "    for x, y in r:\n"
            "        ol.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))\n"
            "    b.Add(z)\n"
        )
    code += f"pcbnew.SaveBoard({str(pcb)!r}, b)\n"
    try:
        return autoroute._pcbnew(python, code)
    finally:
        geometry.unlink(missing_ok=True)


#: How far off a pad's copper a track end may be and still be joined to it: a
#: near miss on a chamfer, not a track that merely ends inside the pad's box.
_SNAP_REACH_MM = 0.15


def _snap_terminals(python: str, pcb: Path, clearance_mm: float = 0.0) -> subprocess.CompletedProcess:
    """Join a track end the router left inside a pad's box but off its copper.

    KRT lands a track on a pad's bounding rectangle. On a chamfered or rounded
    pad, the XIAO's castellations among them, the corner it picks can be the
    one the chamfer cut away: on verdacor's U1 pad 23 GNSS_RXD ended 0.06 mm
    off the copper and the net stayed open on every run, as did others on
    the same pads. A dangling end of the pad's own net that misses its copper
    by no more than ``_SNAP_REACH_MM`` gets a stub to the pad's centre, which
    lies on copper of the same net. The box alone is not enough: a rotated or
    concave pad's box holds space well away from its copper, where another
    net may run (Codex, PR #69). So the whole stub is checked against every
    other net's pads and tracks at the track's own clearance, and against any
    keepout that forbids tracks, and an end it would not clear is left open.
    ``clearance_mm`` is the project's floor, passed in rather than trusted to
    pcbnew, which reads 0 for a board loaded without its project file.
    Prints how many it joined.
    """
    from . import autoroute

    return autoroute._pcbnew(python, (
        "import pcbnew\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        "tracks = [t for t in b.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T]\n"
        "vias = {(v.GetPosition().x, v.GetPosition().y) for v in b.GetTracks() if v.Type() == pcbnew.PCB_VIA_T}\n"
        "others = [t for t in b.GetTracks() if t.Type() in (pcbnew.PCB_TRACE_T, pcbnew.PCB_VIA_T, pcbnew.PCB_ARC_T)]\n"
        "pads = [p for fp in b.GetFootprints() for p in fp.Pads()]\n"
        # Footprint rule areas too: a module's antenna keep-out lives in its
        # footprint, not in b.Zones().
        "areas = [z for z in (list(b.Zones()) + [z for fp in b.GetFootprints() for z in fp.Zones()]) if z.GetIsRuleArea() and z.GetDoNotAllowTracks()]\n"
        f"reach = pcbnew.FromMM({_SNAP_REACH_MM})\n"
        f"floor = pcbnew.FromMM({float(clearance_mm)!r})\n"
        "def clear(stub, layer, net, gap):\n"
        "    for o in others:\n"
        "        if o.GetNetCode() != net and o.IsOnLayer(layer) and o.GetEffectiveShape(layer).Collide(stub, gap):\n"
        "            return False\n"
        "    for o in pads:\n"
        "        if o.GetNetCode() != net and o.IsOnLayer(layer) and o.GetEffectiveShape(layer).Collide(stub, gap):\n"
        "            return False\n"
        "    for z in areas:\n"
        "        if z.IsOnLayer(layer) and z.Outline().Collide(stub, 0):\n"
        "            return False\n"
        "    return True\n"
        "ends = {}\n"
        "for t in tracks:\n"
        "    for p in (t.GetStart(), t.GetEnd()):\n"
        "        k = (t.GetLayer(), p.x, p.y)\n"
        "        ends[k] = ends.get(k, 0) + 1\n"
        "joined = 0\n"
        "for fp in b.GetFootprints():\n"
        "    for pad in fp.Pads():\n"
        "        net = pad.GetNetCode()\n"
        "        if net <= 0:\n"
        "            continue\n"
        "        for layer in (pcbnew.F_Cu, pcbnew.B_Cu):\n"
        "            if not pad.IsOnLayer(layer):\n"
        "                continue\n"
        "            poly = pad.GetEffectivePolygon(layer, pcbnew.ERROR_INSIDE)\n"
        "            box = poly.BBox()\n"
        "            for t in tracks:\n"
        "                if t.GetNetCode() != net or t.GetLayer() != layer:\n"
        "                    continue\n"
        "                half = t.GetWidth() // 2\n"
        "                for p in (t.GetStart(), t.GetEnd()):\n"
        "                    if ends.get((layer, p.x, p.y), 0) > 1 or (p.x, p.y) in vias:\n"
        "                        continue\n"
        "                    if not (box.GetLeft() - half <= p.x <= box.GetRight() + half\n"
        "                            and box.GetTop() - half <= p.y <= box.GetBottom() + half):\n"
        "                        continue\n"
        "                    at = pcbnew.VECTOR2I(p.x, p.y)\n"
        "                    if poly.Collide(at, half) or not poly.Collide(at, half + reach):\n"
        "                        continue\n"
        "                    gap = max(floor, b.GetDesignSettings().m_MinClearance, t.GetOwnClearance(layer))\n"
        "                    if not clear(pcbnew.SHAPE_SEGMENT(at, pad.GetPosition(), t.GetWidth()), layer, net, gap):\n"
        "                        continue\n"
        "                    stub = pcbnew.PCB_TRACK(b)\n"
        "                    stub.SetStart(pcbnew.VECTOR2I(p.x, p.y))\n"
        "                    stub.SetEnd(pad.GetPosition())\n"
        "                    stub.SetWidth(t.GetWidth())\n"
        "                    stub.SetLayer(layer)\n"
        "                    stub.SetNetCode(net)\n"
        "                    b.Add(stub)\n"
        "                    others.append(stub)\n"
        "                    ends[(layer, p.x, p.y)] = 2\n"
        "                    joined += 1\n"
        f"pcbnew.SaveBoard({str(pcb)!r}, b)\n"
        "print(joined)\n"
    ))


#: The longest dangling track removed after routing. Longer, it is a partial
#: route worth keeping for whoever finishes the board by hand.
_STUB_MAX_MM = 1.0


#: Removal rounds before the trim stops: taking a stub can leave the next one
#: dangling, so it repeats, but a chain longer than this is not a stub.
_STUB_ROUNDS = 20


def _trim_dangling(python: str, pcb: Path) -> subprocess.CompletedProcess:
    """Remove the short track stubs the router leaves connected at one end only.

    KRT leaves a few: on verdacor 0.02 to 0.26 mm of GND, VDD_3V3 and
    VBAT_PROT ending in nothing, each a track_dangling warning in DRC and none
    carrying anything. Judged after the zones are filled, with KiCad's own
    test, because a stub into a pour is what connects its pad to the plane,
    and before the fill it looks dangling too. Removed until none is left
    (taking one can leave the next dangling), then the zones refilled around
    the copper that remains. The returned stdout's last line is how many.

    pcbnew only finds them. Removing a track through KiCad 10's binding
    leaves the process unable to walk the board again, or to load another
    (a TypeError, an AttributeError, then a segfault): one round was all the
    ear tag needed, and pathfinder's core, needing a second, failed its route
    there. So each round is its own process that reports the stubs' UUIDs,
    and they come out of the file through the s-expression parser.
    """
    import json

    from blpl.emitter import sexpr

    from . import autoroute

    find = (
        "import json, pcbnew\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        f"limit = pcbnew.FromMM({_STUB_MAX_MM})\n"
        "zones = b.Zones()\n"
        "if len(zones):\n"
        "    pcbnew.ZONE_FILLER(b).Fill(zones)\n"
        "b.BuildConnectivity()\n"
        "conn = b.GetConnectivity()\n"
        "stubs = [t.m_Uuid.AsString() for t in b.GetTracks() if t.Type() == pcbnew.PCB_TRACE_T\n"
        "         and t.GetLength() <= limit and conn.TestTrackEndpointDangling(t, False)]\n"
        "if not stubs:\n"
        # Nothing left: the board is saved filled around what remains.
        f"    pcbnew.SaveBoard({str(pcb)!r}, b)\n"
        "print('STUBS ' + json.dumps(stubs))\n"
    )
    removed = 0
    for _ in range(_STUB_ROUNDS):
        proc = autoroute._pcbnew(python, find)
        line = next((l for l in reversed((proc.stdout or "").splitlines()) if l.startswith("STUBS ")), None)
        if proc.returncode != 0 or line is None:
            return proc
        stubs = set(json.loads(line[len("STUBS "):]))
        if not stubs:
            break
        board = sexpr.parse(pcb.read_text(encoding="utf-8"))
        kept = [n for n in board if not (
            isinstance(n, list) and sexpr.head(n) == "segment"
            and sexpr.unquote(str((sexpr.find(n, "uuid") or [None, ""])[1])) in stubs)]
        gone = len(board) - len(kept)
        if gone == 0:
            break  # nothing matched: stop rather than loop on the same answer
        pcb.write_text(sexpr.dump_top(kept), encoding="utf-8")
        removed += gone
    return subprocess.CompletedProcess(proc.args, 0, f"{removed}\n", proc.stderr)


#: How close a ground via must be to the signal via or capacitor pad it
#: stitches, centre to centre: what Stage 8's RP-001 asks for.
_STITCH_REACH_MM = 1.0


def _stitch(python: str, pcb: Path, *, via_mm: float, drill_mm: float, clearance_mm: float,
            edge_mm: float, hole_mm: float, hole_to_hole_mm: float) -> subprocess.CompletedProcess:
    """Put a plane-net via beside each signal via, and beside each capacitor's plane pad.

    A signal that changes layer through a via changes reference plane with
    it, and its return current needs a way between the planes close by: on
    verdacor, with both inner layers GND, 18 signal vias had none within
    1 mm (RP-001), and C6 and C7, U3's decoupling, reached a ground via only
    through a long track (DC-003). The router does not place these.

    The plane nets are those with a zone on an inner layer. A signal via is
    stitched to the ground one; a capacitor pad on a plane net to its own net.
    For each signal via with no plane-net via, and each such pad with no via of
    its own net, within ``_STITCH_REACH_MM``, candidate spots on rings around it are tried
    nearest first; a via goes in the first that clears every other net's
    tracks, vias and pads on every copper layer the via passes, at the larger
    of the two items' class clearances, never under ``clearance_mm`` (as DRC
    judges a pair), stays off every pad (no via in a pad), keeps ``edge_mm``
    from the board edge and its cutouts, the hole clearances from
    every hole, is in no keepout that forbids vias, and is inside one of its
    net's inner-layer zones (and, after the refill, in that zone's fill, or it
    is taken out again: a via outside a partial plane would float). A
    capacitor's via is joined to its pad by a track that has to clear the same
    way and cross no keepout that forbids tracks. Where no spot
    clears, nothing is placed: an unstitched via is a review finding, a via
    shorted to a neighbour is a scrapped board. The zones are refilled
    afterwards. Prints "placed missed".
    """
    from . import autoroute

    return autoroute._pcbnew(python, (
        "import math, pcbnew\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        "mm = pcbnew.FromMM\n"
        f"VR, DR = mm({via_mm} / 2), mm({drill_mm} / 2)\n"
        f"CLR, EDGE, HOLE, H2H = mm({clearance_mm}), mm({edge_mm}), mm({hole_mm}), mm({hole_to_hole_mm})\n"
        f"REACH = mm({_STITCH_REACH_MM})\n"
        "copper = list(b.GetEnabledLayers().CuStack())\n"
        "inner = [l for l in copper if l not in (pcbnew.F_Cu, pcbnew.B_Cu)]\n"
        "planes = {z.GetNetCode() for z in b.Zones() if not z.GetIsRuleArea() and z.GetNetCode() > 0\n"
        "          and any(z.IsOnLayer(l) for l in inner)}\n"
        "if not planes:\n"
        "    print('0 0'); raise SystemExit(0)\n"
        "names = {c: b.GetNetInfo().GetNetItem(c).GetNetname() for c in planes}\n"
        "# A signal via is stitched to ground. A capacitor pad keeps its own\n"
        "# plane net: GND to GND, VDD to VDD, never one onto the other (Codex, PR #72).\n"
        "GROUND = sorted(planes, key=lambda c: (not names[c].upper().startswith(\n"
        "    ('GND', 'VSS', 'AGND', 'DGND', 'PGND')), c))[0]\n"
        "outline = pcbnew.SHAPE_POLY_SET()\n"
        "b.GetBoardPolygonOutlines(outline, True)\n"
        "edges = []\n"
        "for i in range(outline.OutlineCount()):\n"
        "    # Cutouts too: they are board edge, as the routing band treats them.\n"
        "    for ch in [outline.Outline(i)] + [outline.Hole(i, j) for j in range(outline.HoleCount(i))]:\n"
        "        pts = [ch.CPoint(k) for k in range(ch.PointCount())]\n"
        "        edges += [(pts[k], pts[(k + 1) % len(pts)]) for k in range(len(pts))]\n"
        "def edge_distance(at):\n"
        "    # SHAPE_POLY_SET's distance is 0 anywhere inside: the edge, measured here.\n"
        "    best = None\n"
        "    for a, c in edges:\n"
        "        dx, dy = c.x - a.x, c.y - a.y\n"
        "        L = dx * dx + dy * dy\n"
        "        t = 0.0 if L == 0 else max(0.0, min(1.0, ((at.x - a.x) * dx + (at.y - a.y) * dy) / L))\n"
        "        d = math.hypot(at.x - a.x - t * dx, at.y - a.y - t * dy)\n"
        "        best = d if best is None or d < best else best\n"
        "    return best if best is not None else 0\n"
        # The footprints' rule areas as well as the board's: b.Zones() holds
        # only board-level zones, so a keep-out a footprint carries (the
        # BM20BC's antenna area, ANT_LRHF's body) was invisible here and a
        # ground via went into it (verdacor-ear-tag-bm20, DRC
        # items_not_allowed).
        "rule_areas = (list(b.Zones()) + [z for fp in b.GetFootprints() for z in fp.Zones()])\n"
        "keepouts = [z for z in rule_areas if z.GetIsRuleArea() and z.GetDoNotAllowVias()]\n"
        "no_tracks = [z for z in rule_areas if z.GetIsRuleArea() and z.GetDoNotAllowTracks()]\n"
        "plane_zones = [z for z in b.Zones() if not z.GetIsRuleArea() and z.GetNetCode() in planes]\n"
        "def in_plane(at, net):\n"
        "    # Inside one of the net's own inner-layer zones: a partial plane is\n"
        "    # allowed, and a via outside it would float (Codex, PR #72).\n"
        "    return any(z.GetNetCode() == net and any(z.IsOnLayer(l) and z.Outline().Contains(at) for l in inner)\n"
        "               for z in plane_zones)\n"
        "def link_ok(link, layer):\n"
        "    # A link crosses no keepout that forbids tracks, as _snap_terminals checks.\n"
        "    return not any(z.IsOnLayer(layer) and z.Outline().Collide(link, 0) for z in no_tracks)\n"
        "pads = [p for fp in b.GetFootprints() for p in fp.Pads()]\n"
        "items = [t for t in b.GetTracks()]\n"
        "holes = [(v.GetPosition(), v.GetDrillValue() // 2) for v in items if v.Type() == pcbnew.PCB_VIA_T]\n"
        "holes += [(p.GetPosition(), max(p.GetDrillSize().x, p.GetDrillSize().y) // 2) for p in pads\n"
        "          if p.GetDrillSize().x > 0]\n"
        "own_cache = {}\n"
        "def own(code):\n"
        "    # A net's own class clearance (GND in Power_Bulk at 0.25 on verdacor,\n"
        "    # over the Default 0.15): DRC takes the larger of the two items' clearances.\n"
        "    if code not in own_cache:\n"
        "        probe = pcbnew.PCB_VIA(b)\n"
        "        b.Add(probe)\n"
        "        probe.SetNetCode(code)\n"
        "        own_cache[code] = max(CLR, probe.GetOwnClearance(pcbnew.F_Cu))\n"
        "        b.Remove(probe)\n"
        "    return own_cache[code]\n"
        "def stitched(at, nets):\n"
        "    return any(v.Type() == pcbnew.PCB_VIA_T and v.GetNetCode() in nets\n"
        "               and (v.GetPosition() - at).EuclideanNorm() <= REACH for v in items)\n"
        "def clears(shape, layers, net, pads_same_too):\n"
        "    gap = own(net)\n"
        "    for t in items:\n"
        "        if t.GetNetCode() == net:\n"
        "            continue\n"
        "        for l in layers:\n"
        "            if t.IsOnLayer(l) and t.GetEffectiveShape(l).Collide(shape, max(gap, t.GetOwnClearance(l))):\n"
        "                return False\n"
        "    for p in pads:\n"
        "        same = p.GetNetCode() == net\n"
        "        if same and not pads_same_too:\n"
        "            continue\n"
        "        for l in layers:\n"
        "            if p.IsOnLayer(l) and p.GetEffectiveShape(l).Collide(shape, 0 if same else max(gap, p.GetOwnClearance(l))):\n"
        "                return False\n"
        "    return True\n"
        "def spot_ok(at, net):\n"
        "    if not outline.Contains(at) or edge_distance(at) < EDGE + VR:\n"
        "        return False\n"
        "    if not in_plane(at, net):\n"
        "        return False\n"
        "    for z in keepouts:\n"
        "        if z.Outline().Collide(pcbnew.VECTOR2I(at), VR):\n"
        "            return False\n"
        "    for c, r in holes:\n"
        "        d = (c - at).EuclideanNorm()\n"
        "        if d < r + DR + H2H or d < r + VR + HOLE:\n"
        "            return False\n"
        "    # A through via is copper on every layer it passes: an inner signal\n"
        "    # layer's tracks count as much as the outer ones' (Codex, PR #72).\n"
        "    return clears(pcbnew.SHAPE_CIRCLE(at, VR), copper, net, True)\n"
        "def place(at, net, link_from=None, width=0, link_layer=None):\n"
        "    v = pcbnew.PCB_VIA(b)\n"
        "    b.Add(v)\n"
        "    v.SetPosition(at); v.SetWidth(2 * VR); v.SetDrill(2 * DR)\n"
        "    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu); v.SetNetCode(net)\n"
        "    items.append(v); holes.append((at, DR))\n"
        "    added.append([v])\n"
        "    if link_from is not None:\n"
        "        t = pcbnew.PCB_TRACK(b)\n"
        "        b.Add(t)\n"
        "        t.SetStart(link_from); t.SetEnd(at); t.SetWidth(width)\n"
        "        t.SetLayer(link_layer); t.SetNetCode(net)\n"
        "        items.append(t); added[-1].append(t)\n"
        "def ring(c, radii):\n"
        "    for r in radii:\n"
        "        for k in range(24):\n"
        "            a = 2 * math.pi * k / 24\n"
        "            yield pcbnew.VECTOR2I(int(c.x + r * math.cos(a)), int(c.y + r * math.sin(a)))\n"
        "placed = missed = 0\n"
        "added = []\n"
        "for v in [t for t in items if t.Type() == pcbnew.PCB_VIA_T and t.GetNetCode() not in planes\n"
        "          and t.GetNetCode() > 0]:\n"
        "    c = v.GetPosition()\n"
        "    if stitched(c, planes):\n"
        "        continue\n"
        "    lo = v.GetWidth(pcbnew.F_Cu) // 2 + VR + max(own(GROUND), v.GetOwnClearance(pcbnew.F_Cu))\n"
        "    for at in ring(c, [r for r in (lo, lo + mm(0.1), REACH) if r <= REACH]):\n"
        "        if spot_ok(at, GROUND):\n"
        "            place(at, GROUND); placed += 1; break\n"
        "    else:\n"
        "        missed += 1\n"
        "for p in pads:\n"
        "    fp = p.GetParentFootprint()\n"
        "    net = p.GetNetCode()\n"
        "    if net not in planes or not fp.GetReference().startswith('C') or p.GetDrillSize().x > 0:\n"
        "        continue\n"
        "    c = p.GetPosition()\n"
        "    if stitched(c, {net}):\n"
        "        continue\n"
        "    link_layer = pcbnew.F_Cu if p.IsOnLayer(pcbnew.F_Cu) else pcbnew.B_Cu\n"
        "    half = max(p.GetSize(link_layer).x, p.GetSize(link_layer).y) // 2\n"
        "    width = min(p.GetSize(link_layer).x, p.GetSize(link_layer).y, mm(0.3))\n"
        "    lo = half + VR + mm(0.05)\n"
        "    for at in ring(c, [r for r in (lo, lo + mm(0.1), REACH) if r <= REACH]):\n"
        "        link = pcbnew.SHAPE_SEGMENT(c, at, width)\n"
        "        if spot_ok(at, net) and clears(link, (link_layer,), net, False) and link_ok(link, link_layer):\n"
        "            place(at, net, c, width, link_layer); placed += 1; break\n"
        "    else:\n"
        "        missed += 1\n"
        "zones = b.Zones()\n"
        "if len(zones):\n"
        "    pcbnew.ZONE_FILLER(b).Fill(zones)\n"
        "# Kept only where the fill actually connects it, by KiCad's own reckoning\n"
        "# (a via joins through thermal spokes, its centre in a gap of the fill):\n"
        "# a via inside the zone's outline can still sit in a void, and float.\n"
        "b.BuildConnectivity()\n"
        "conn = b.GetConnectivity()\n"
        "def joined(v):\n"
        "    return any(it.Type() == pcbnew.PCB_ZONE_T and it.GetNetCode() == v.GetNetCode()\n"
        "               for it in conn.GetConnectedItems(v))\n"
        # And anchored on two layers or more: a via touching one plane and
        # nothing else connects nothing, and DRC calls it dangling. On
        # pathfinder's core GND is a plane on In1.Cu alone (In2.Cu is 3V3), and
        # 130 such vias went in. Counted from what touches the via, not from
        # GetConnectedItems, which answers with the whole connected net.
        "def anchors(v):\n"
        "    at, code, r = v.GetPosition(), v.GetNetCode(), v.GetWidth(pcbnew.F_Cu) // 2\n"
        "    layers, plane = set(), False\n"
        "    for z in plane_zones:\n"
        "        if z.GetNetCode() != code:\n"
        "            continue\n"
        "        for l in z.GetLayerSet().Seq():\n"
        "            if v.IsOnLayer(l) and z.HitTestFilledArea(l, at, r):\n"
        "                layers.add(l)\n"
        "                plane = True\n"
        # The tracks as listed before any probe was added and taken away (a
        # board is not walked again after a Remove), and the links placed here.
        # Anywhere along a track counts, not only its ends: spot_ok lets a
        # via land partway along a same-net track, and that joins its layer
        # as surely as an end does (Codex, PR #83).
        "    for t in items + [it for g in added for it in g]:\n"
        "        if t.Type() in (pcbnew.PCB_TRACE_T, pcbnew.PCB_ARC_T) and t.GetNetCode() == code and t.HitTest(at, r):\n"
        "            layers.add(t.GetLayer())\n"
        "    for p in pads:\n"
        "        if p.GetNetCode() == code and p.HitTest(at):\n"
        "            layers.update(l for l in p.GetLayerSet().Seq() if pcbnew.IsCopperLayer(l) and v.IsOnLayer(l))\n"
        "    return plane and len(layers) >= 2\n"
        "def filled(v):\n"
        "    return joined(v) and anchors(v)\n"
        "orphans = [group for group in added if not filled(group[0])]\n"
        "for group in orphans:\n"
        "    for item in group:\n"
        "        b.Remove(item)\n"
        "placed -= len(orphans); missed += len(orphans)\n"
        "if orphans and len(zones):\n"
        "    pcbnew.ZONE_FILLER(b).Fill(zones)\n"
        f"pcbnew.SaveBoard({str(pcb)!r}, b)\n"
        "print(placed, missed)\n"
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


def _off_layer(node, allowed: list[str]) -> list[str]:
    """The layers a track or via puts copper on that ``allowed`` does not include.

    A via is judged by both its ends, and on a net held to one layer any via
    at all is off it: a via exists only to leave the layer it starts on.
    """
    from ..emitter import sexpr

    if sexpr.head(node) == "via":
        layers = sexpr.find(node, "layers")
        ends = [sexpr.unquote(str(l)) for l in (layers or [])[1:]] or ["F.Cu", "B.Cu"]
        off = [l for l in ends if l not in allowed]
        return off or (ends if len(allowed) < 2 else [])
    layer = sexpr.find(node, "layer")
    name = sexpr.unquote(str(layer[1])) if layer is not None and len(layer) > 1 else "?"
    return [] if name in allowed else [name]


def _restricted_nets(pcb: Path, class_layers: dict[str, list[str]] | None) -> dict[str, tuple[str, list[str]]]:
    """Each net whose class ``class_layers`` holds to layers: net -> (class, layers)."""
    if not class_layers:
        return {}
    groups, _ = nets_by_class(pcb, pcb.with_suffix(".kicad_pro"))
    return {net: (name, list(layers)) for name, layers in class_layers.items() if layers
            for net in groups.get(name, [])}


def _strays(pcb: Path, class_layers: dict[str, list[str]] | None):
    """The parsed board, and each track or via on a layer-restricted net that
    leaves its class's layers, as (index in the board, net, class, allowed,
    off-layers). (None, []) when nothing is restricted."""
    from ..emitter import sexpr

    restricted = _restricted_nets(pcb, class_layers)
    if not restricted:
        return None, []
    board = sexpr.parse(pcb.read_text(encoding="utf-8", errors="replace"))
    # Both spellings of an item's net: KiCad 10's (net "NAME") and the older
    # (net 5), read through the top-level table. Matching one form only is how
    # a routed board was once reported to have no netlist at all.
    numbered = {str(n[1]): sexpr.unquote(str(n[2])) for n in sexpr.find_all(board, "net") if len(n) >= 3}
    out = []
    for i, node in enumerate(board):
        if sexpr.head(node) not in ("segment", "arc", "via"):
            continue
        tag = sexpr.find(node, "net")
        if tag is None or len(tag) < 2:
            continue
        value = str(tag[-1])
        net = sexpr.unquote(value) if value.startswith('"') else numbered.get(value)
        if net not in restricted:
            continue
        name, allowed = restricted[net]
        off = _off_layer(node, allowed)
        if off:
            out.append((i, net, name, allowed, off))
    return board, out


def layer_violations(pcb: Path, class_layers: dict[str, list[str]] | None) -> list[dict]:
    """Copper on a layer-restricted net that is off the layers its class allows.

    One entry per offending net: its class, the allowed layers, the length of
    track on each layer it should not be on, and how many of its vias leave
    the allowed layers. Empty when nothing strays -- and when nothing is
    restricted, so an empty list is a pass only where ``class_layers`` named
    a class with nets.

    Read from the board file after the router and every BLPL pass after it,
    because that is what ships. verdacor-ear-tag-bm20 shipped its 2.4 GHz
    RF_LRHF, of a class held to F.Cu over the In1.Cu ground, with 22 mm of
    it on B.Cu and four vias, and the one number anyone looked at -- open
    connections -- had improved.
    """
    from ..emitter import sexpr

    board, strays = _strays(pcb, class_layers)
    found: dict[str, dict] = {}
    for i, net, name, allowed, off in strays:
        entry = found.setdefault(net, {"net": net, "class": name, "allowed": allowed, "tracks_mm": {}, "vias": 0})
        node = board[i]
        if sexpr.head(node) == "via":
            entry["vias"] += 1
            continue
        # Chords through start, mid and end: a figure for the report, not geometry.
        pts = [p for p in (sexpr.find(node, k) for k in ("start", "mid", "end")) if p is not None and len(p) >= 3]
        pts = [(float(p[1]), float(p[2])) for p in pts]
        mm = sum(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(pts, pts[1:]))
        entry["tracks_mm"][off[0]] = entry["tracks_mm"].get(off[0], 0.0) + mm
    for entry in found.values():
        entry["tracks_mm"] = {k: round(v, 2) for k, v in sorted(entry["tracks_mm"].items())}
    return [found[n] for n in sorted(found)]


def _enforce_layers(pcb: Path, result, class_layers: dict[str, list[str]] | None, work: Path):
    """Hold the kept board to its classes' layers, and say so loudly when it is not.

    Attempts are ranked clean-first before this runs, so violations here mean
    no attempt kept the restricted nets on their layers. The off-layer copper
    is then **stripped** and those nets read as open, rather than shipped.
    Deliberately: copper on the wrong layer is connected copper. The open
    count, Stage 7's DRC (which knows nothing of an impedance plan) and
    Stage 8 all read such a net as done, and a 50 ohm trace that is not
    50 ohm is invisible to every one of them -- exactly how RF_LRHF went out
    on B.Cu. An open is the one failure every later stage already reports,
    and twenty millimetres of RF is quicker to route by hand on the right
    layer than to find on the wrong one. The board as routed is kept beside
    the attempts as ``<board>.layer-violations.kicad_pcb``.
    """
    from ..emitter import sexpr
    from . import autoroute

    if not result.ok or not class_layers:
        return result
    found = layer_violations(pcb, class_layers)
    # How many nets were held to their layers, beside the verdict: an empty
    # violation list over no nets at all would read as a pass.
    result.extra["layer_checked_nets"] = len(_restricted_nets(pcb, class_layers))
    result.extra["layer_violations"] = found
    if not found:
        return result
    work.mkdir(parents=True, exist_ok=True)
    routed = work / f"{pcb.stem}.layer-violations.kicad_pcb"
    shutil.copyfile(pcb, routed)
    board, strays = _strays(pcb, class_layers)
    # Through the s-expression parser, as _trim_dangling removes its stubs and
    # for its reason: removing a track through KiCad 10's binding leaves the
    # process unable to walk the board again.
    drop = {i for i, *_ in strays}
    pcb.write_text(sexpr.dump_top([n for i, n in enumerate(board) if i not in drop]), encoding="utf-8")
    left = layer_violations(pcb, class_layers)
    nets = "; ".join(
        f"{e['net']} (class {e['class']}, allowed {'/'.join(e['allowed'])}: "
        + ", ".join([f"{mm} mm on {layer}" for layer, mm in e["tracks_mm"].items()]
                    + ([f"{e['vias']} via(s)"] if e["vias"] else []))
        + ")" for e in found)
    result.extra["layer_violations_stripped"] = len(drop)
    result.extra["layer_violations_board"] = str(routed)
    result.extra["layer_warning"] = (
        f"LAYER RESTRICTION BROKEN on every attempt: {nets}. The {len(drop)} off-layer track(s) "
        f"and via(s) were removed, so these nets read as open instead of shipping on the wrong "
        f"layer; the board as routed is {routed.name}")
    # A count that drops to zero is checked for what it counts: re-read from
    # disk, nothing the check finds may be left.
    if left:
        result.ok = False
        result.reason = (f"{result.extra['layer_warning']}; but {len(left)} net(s) still stray after "
                         "the strip, so the board is not trusted")
        return result
    # The pour is refilled into the space the stripped copper left, and the
    # board measured again: its open count is now the true one.
    pro = pcb.with_suffix(".kicad_pro")
    kept_pro = work / f"{pcb.stem}.kicad_pro.layer-guard"
    if pro.is_file():
        shutil.copyfile(pro, kept_pro)
    python = autoroute.find_pcbnew_python()
    filled = _fill(python, pcb)
    if kept_pro.is_file():
        shutil.copyfile(kept_pro, pro)  # pcbnew's save rewrites it; see the module docstring
    if filled.returncode != 0:
        result.ok = False
        result.reason = f"{result.extra['layer_warning']}; and pcbnew could not refill the zones: {autoroute._msg(filled)}"
        return result
    measured = autoroute._measure_board(python, pcb)
    if measured is None:
        result.ok = False
        result.reason = f"{result.extra['layer_warning']}; and the stripped board could not be measured"
        return result
    result.unrouted = measured.get("unconnected")
    result.segments = measured.get("segments")
    return result


def route(pcb: Path, *, work_dir: Path, timeout: int = 3600,
          class_layers: dict[str, list[str]] | None = None,
          search: dict | None = None, retry: list[dict] | None = None,
          fanout: list[dict] | None = None):
    """Route ``pcb`` in place with KiCad Routing Tools; the board's own measure.

    ``search`` is routing.search, on every attempt. ``retry`` is
    routing.retry: when a route leaves connections open, the board goes back
    to how it was before routing and is routed again with each entry laid over
    ``search`` in turn, until one leaves nothing open. The first such route is
    kept, or failing that the one with the fewest open, the earliest on a tie.
    Every attempt is in ``extra["attempts"]``, so the report says which
    settings the kept board came from and what the others left.

    ``class_layers`` is checked on the result, not only handed to the router
    as a flag: an attempt that leaves a restricted net off its layers ranks
    below every attempt that does not, and when every attempt did, the kept
    board has that copper stripped and the report says so loudly
    (``_enforce_layers``).
    """
    from . import autoroute

    # Every attempt's settings checked before anything touches the board. A
    # misspelt key found in plan() came after the edge band was drawn, so the
    # error left the band on the board and skipped run_for's report (Codex,
    # PR #95); refused here, the board is as it was and the report says why.
    try:
        for extra in [{}, *(retry or [])]:
            search_args({**(search or {}), **extra})
        check_fanout(fanout)
    except ValueError as exc:
        return autoroute.RouteResult(False, str(exc), pcb=str(pcb), extra={"router": "kicad-routing-tools"})
    pcb = Path(pcb)
    work = Path(work_dir)
    if not retry:
        result = _route_once(pcb, work_dir=work, timeout=timeout,
                             class_layers=class_layers, search=search, fanout=fanout)
        return _enforce_layers(pcb, result, class_layers, work)
    work.mkdir(parents=True, exist_ok=True)
    # Every attempt starts from this, not from the last attempt's copper:
    # routed over, a retry would be finishing someone else's board.
    start = work / f"{pcb.stem}.unrouted.kicad_pcb"
    shutil.copyfile(pcb, start)
    attempts: list[dict] = []
    best = None
    result = None
    for i, extra in enumerate([{}, *retry]):
        settings = {**(search or {}), **extra}
        if i:
            shutil.copyfile(start, pcb)
        result = _route_once(pcb, work_dir=work, timeout=timeout,
                             class_layers=class_layers, search=settings, fanout=fanout)
        if not result.attempted:
            return result  # not installed: no setting changes that
        # Measured on the board this attempt left, before anything ranks it:
        # an attempt with a restricted net off its layers loses to every
        # attempt without, however many fewer connections it left open.
        # ear-tag-bm20 kept the attempt that left 9 open over the one that
        # left 10, and the 9 was the one with RF_LRHF on B.Cu.
        strays = layer_violations(pcb, class_layers) if result.ok else []
        attempts.append({"search": settings, "ok": result.ok, "unrouted": result.unrouted,
                         **({"layer_violations": [e["net"] for e in strays]} if strays else {}),
                         **({} if result.ok else {"reason": result.reason})})
        rank = (bool(strays), result.unrouted or 0)
        if result.ok and (best is None or rank < best[3]):
            kept = work / f"{pcb.stem}.attempt{i}.kicad_pcb"
            shutil.copyfile(pcb, kept)
            best = (kept, result, i, rank)
        if result.ok and not result.unrouted and not strays:
            break
    if best is None:
        # Nothing routed at all: the last failure, with every attempt's reason.
        result.extra["attempts"] = attempts
        return result
    kept, result, i, _ = best
    shutil.copyfile(kept, pcb)
    result.extra["attempts"] = attempts
    result.extra["kept_attempt"] = i
    return _enforce_layers(pcb, result, class_layers, work)


#: KRT's own record of nets its rip machinery must leave alone, in the
#: ``.kicad_pro`` beside the board (its protected_nets.py, issue #521).
KRT_NAMESPACE = "kicad_routing_tools"
KRT_PROTECTED = "protected_nets"
LAYER_PROTECTION = "layer-restricted (BLPL)"


def _protect_restricted(pcb: Path, class_layers: dict[str, list[str]] | None) -> str | None:
    """Mark every layer-restricted net protected in the project file KRT reads;
    the file's text as it was, to put back, or None when nothing was marked.

    The ``!`` that keeps an RF net out of a later pass does not keep KRT's
    hands off it. On ear-tag-bm20 the final signal pass, which also carries
    GND, ran KRT's in-run plane finalize (#562); its pad repair for C12.2
    ripped the blockers in its way -- VBAT_RAW, LR_NRESET and RF_LRHF, all
    three excluded from that pass -- and reconnected RF_LRHF with that
    pass's layers (F.Cu and B.Cu) and its 0.2 mm width: 22 mm on B.Cu and
    four vias, on a 50 ohm net held to F.Cu. That rip honours KRT's
    protection list and nothing else, so the list is where BLPL says it.
    Protection only stops ripping: a net named exactly in --nets still
    routes in its own pass, which is how every restricted class is routed.
    The guard in ``route`` remains the check; this keeps it from firing.
    """
    restricted = _restricted_nets(pcb, class_layers)
    pro = pcb.with_suffix(".kicad_pro")
    if not restricted or not pro.is_file():
        return None
    text = pro.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError:
        return None
    ns = data.setdefault(KRT_NAMESPACE, {})
    marked = ns.setdefault(KRT_PROTECTED, {})
    for net in restricted:
        marked.setdefault(net, LAYER_PROTECTION)
    # KRT reads the newer of the project's copy and one on the board by this
    # revision; one past whatever is here keeps the project's copy the winner.
    try:
        ns["records_rev"] = int(ns.get("records_rev") or 0) + 1
    except (TypeError, ValueError):
        ns["records_rev"] = 1
    pro.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return text


def _route_once(pcb: Path, *, work_dir: Path, timeout: int = 3600,
                class_layers: dict[str, list[str]] | None = None,
                search: dict | None = None, fanout: list[dict] | None = None):
    """One route of the board, with its layer-restricted nets protected from
    KRT's rip-up while it runs and the project file put back after."""
    pcb = Path(pcb)
    original = _protect_restricted(pcb, class_layers)
    try:
        return _route_steps(pcb, work_dir=work_dir, timeout=timeout,
                            class_layers=class_layers, search=search, fanout=fanout)
    finally:
        if original is not None:
            pcb.with_suffix(".kicad_pro").write_text(original, encoding="utf-8")


def _route_steps(pcb: Path, *, work_dir: Path, timeout: int = 3600,
                 class_layers: dict[str, list[str]] | None = None,
                 search: dict | None = None, fanout: list[dict] | None = None):
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
        hole = rules.get("min_hole_clearance")
        ring = (hole + widest / 2) if isinstance(hole, (int, float)) and hole > 0 else 0.0
        banded = _edge_band(python, pcb, edge + widest / 2, add=True, hole_ring_mm=ring)
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
                 class_layers=class_layers, search=search, fanout=fanout)
    if refused:
        result.extra["refused"] = refused
    result.extra["steps"] = [" ".join([s["script"], *s["args"]]) for s in steps]
    bands = False
    widest_via = max([c.get("via_diameter", 0.6) for c in classes.values()] or [0.6])
    widest_gap = max([c.get("clearance", 0.2) for c in classes.values()] + [rules.get("min_clearance") or 0.0])

    def unband() -> str:
        if not bands:
            return ""
        off = _via_band_areas(python, pcb, None)
        restore_pro()
        return "" if off.returncode == 0 else f"pcbnew could not remove the via bands: {autoroute._msg(off)}"

    for step in steps:
        if step["script"] == VIA_BANDS:
            rects = via_bands(pcb.read_text(encoding="utf-8"), step["args"],
                              via_mm=widest_via, clearance_mm=widest_gap)
            drawn = _via_band_areas(python, pcb, rects)
            restore_pro()
            bands = True
            if drawn.returncode != 0:
                left = unband()
                result.reason = f"pcbnew could not draw the via bands: {autoroute._msg(drawn)}" + (f"; and {left}" if left else "")
                return result
            result.extra["via_bands"] = len(rects)
            log.append(f"$ via bands beside {', '.join(step['args'])}: {len(rects)} area(s)")
            continue
        script = str(home / "py_router" / step["script"])
        target = ([str(pcb), str(pcb)] if step["script"] == "route_planes.py"
                  else [str(pcb), "--output", str(pcb)] if step["script"] == FANOUT
                  else [str(pcb), "-O"])
        if step.get("optional"):
            # A defaulted fan-out (every U part) may meet a part it cannot
            # handle; the board goes back to how it was and the route goes on.
            before = work / f"{pcb.stem}.pre-fanout.kicad_pcb"
            shutil.copyfile(pcb, before)
        proc = _run([sys.executable, script, *target, *step["args"]], timeout)
        restore_pro()
        log.append(f"$ {step['script']} {' '.join(step['args'])}\n{(proc.stdout or '')[-4000:]}{(proc.stderr or '')[-2000:]}")
        if proc.returncode != 0 and step.get("optional"):
            shutil.copyfile(before, pcb)
            ref = step["args"][step["args"].index("--component") + 1]
            result.extra.setdefault("fanout_skipped", []).append(
                {"ref": ref, "reason": f"exited {proc.returncode}: {autoroute._msg(proc)[-300:]}"})
            continue
        if proc.returncode != 0:
            left = unband()
            result.reason = f"{step['script']} exited {proc.returncode}: {autoroute._msg(proc)[-800:]}" + (f"; and {left}" if left else "")
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
    # Off before the stitching pass: a ground via beside an RF trace is what
    # the band is for, not what it keeps out.
    unbanded_vias = unband()
    if unbanded_vias:
        result.reason = unbanded_vias
        return result
    unbanded = _edge_band(python, pcb, 0, add=False) if band else subprocess.CompletedProcess([], 0, "", "")
    restore_pro()
    if unbanded.returncode != 0:
        result.reason = f"pcbnew could not remove the edge band: {autoroute._msg(unbanded)}"
        return result
    floor = max([v for v in (rules.get("min_clearance"), classes.get("Default", {}).get("clearance"))
                 if isinstance(v, (int, float))] or [0.0])
    snapped = _snap_terminals(python, pcb, floor)
    restore_pro()
    if snapped.returncode != 0:
        result.reason = f"pcbnew could not join track ends to their pads: {autoroute._msg(snapped)}"
        return result
    joined = ((snapped.stdout or "").strip().splitlines() or ["0"])[-1]
    if joined.isdigit() and int(joined):
        result.extra["terminals_joined"] = int(joined)
    # The router leaves its zones unfilled, and an unfilled plane connects
    # nothing: measured as it stands, every plane pad would read as open.
    filled = _fill(python, pcb)
    restore_pro()
    if filled.returncode != 0:
        result.reason = f"pcbnew could not fill the zones: {autoroute._msg(filled)}"
        return result
    trimmed = _trim_dangling(python, pcb)
    restore_pro()
    if trimmed.returncode != 0:
        result.reason = f"pcbnew could not remove dangling track stubs: {autoroute._msg(trimmed)}"
        return result
    stubs = ((trimmed.stdout or "").strip().splitlines() or ["0"])[-1]
    if stubs.isdigit() and int(stubs):
        result.extra["stubs_removed"] = int(stubs)
    # Ground vias beside signal vias and decoupling pads, at the project's own
    # rules: the default class's via and the board's clearances.
    default_class = classes.get("Default", {})
    stitched = _stitch(
        python, pcb,
        via_mm=default_class.get("via_diameter", 0.6), drill_mm=default_class.get("via_drill", 0.3),
        clearance_mm=floor,
        edge_mm=edge if isinstance(edge, (int, float)) else 0.5,
        hole_mm=rules.get("min_hole_clearance", 0.25), hole_to_hole_mm=rules.get("min_hole_to_hole", 0.25))
    restore_pro()
    if stitched.returncode != 0:
        result.reason = f"pcbnew could not place ground stitching vias: {autoroute._msg(stitched)}"
        return result
    counts = ((stitched.stdout or "").strip().splitlines() or ["0 0"])[-1].split()
    if len(counts) == 2 and all(c.isdigit() for c in counts):
        result.extra["stitching_vias"] = {"placed": int(counts[0]), "no_room": int(counts[1])}
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
