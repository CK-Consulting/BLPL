"""Silkscreen policy, applied to each footprint as it is emitted.

What a board's silkscreen carries is a design decision, and on a small dense
board most of it is noise that DRC then counts: on verdacor-ear-tag 58
silk_over_copper and 13 silk_overlap findings, 45 of the first from two
vendor modules' own outlines drawn across their pads, the rest reference
designators on parts too crowded to carry them. project.yaml's ``silkscreen``
says what the board should carry instead:

- ``references: hidden`` — no reference designators in silk. They stay on
  F.Fab, the assembly drawing, which is where a placement operator reads them.
  ``fit`` (the default) keeps the emitter's existing rule: a reference stays
  only when it fits on its own part.
- ``labels: {REF: text}`` — the markings a person must read on the finished
  board (a battery's polarity, an antenna connector's band), in silk where the
  part's reference would have been, at ``label_size``.
- ``clip_to_copper: true`` — a footprint's silk graphics are trimmed where
  they cross that footprint's own pads. The fab clips silk at mask openings
  anyway; trimming it here makes the board match what will be printed, and
  keeps the rest of an outline (and its pin-1 mark) rather than dropping it.
"""

from __future__ import annotations

import math

from . import ids, sexpr

#: Silk keeps this far from a pad's copper, beyond half its own stroke: the
#: solder-mask expansion a fab opens around a pad.
PAD_MARGIN = 0.05

#: Sampling step along silk graphics, mm, and the shortest piece worth keeping.
_STEP = 0.05
_MIN_KEEP = 0.2

#: The board's minimum silk text height, KiCad's default.
MIN_TEXT = 0.8

_GRAPHICS = ("fp_line", "fp_arc", "fp_circle", "fp_rect", "fp_poly")


def _num(node, i: int, default: float = 0.0) -> float:
    try:
        return float(node[i])
    except (IndexError, TypeError, ValueError):
        return default


def _layer_names(node) -> list[str]:
    out: list[str] = []
    for tag in ("layer", "layers"):
        for n in sexpr.find_all(node, tag):
            out += [sexpr.unquote(str(x)) for x in n[1:] if isinstance(x, str)]
    return out


def _silk_side(node) -> str | None:
    for name in _layer_names(node):
        if name in ("F.SilkS", "F.Silkscreen"):
            return "F"
        if name in ("B.SilkS", "B.Silkscreen"):
            return "B"
    return None


def _rotate(x: float, y: float, ang: float) -> tuple[float, float]:
    """KiCad's rotation by ``ang`` degrees: counter-clockwise on screen, Y down.

    Checked against pcbnew: a 2 x 0.4 mm pad at 30 degrees covers (0.78, -0.45)
    and not (0.78, 0.45). In a Y-up frame the same equations read as the
    opposite rotation, which is how they get mistaken for a sign error.
    """
    if not ang:
        return x, y
    a = math.radians(ang)
    return x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a)


def _circle(cx: float, cy: float, r: float, n: int = 24) -> list[tuple[float, float]]:
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def _primitives(pad) -> list[tuple[list[tuple[float, float]], float]]:
    """A custom pad's primitives as (polygon, half stroke), in the pad's own frame.

    Their copper reaches past the anchor's ``size``: FlipProbe's pad 5 has a
    0.4 mm anchor and a polygon out to 0.6 mm, and the anchor alone left silk
    crossing that copper unclipped (Codex, PR #64).
    """
    out = []
    prims = sexpr.find(pad, "primitives")
    for g in (prims[1:] if prims is not None else []):
        if not isinstance(g, list):
            continue
        tag, half = sexpr.head(g), _stroke(g) / 2 if sexpr.find(g, "width") or sexpr.find(g, "stroke") else 0.0
        if tag == "gr_poly":
            pts = [(_num(xy, 1), _num(xy, 2)) for n in sexpr.find_all(g, "pts") for xy in sexpr.find_all(n, "xy")]
        elif tag == "gr_rect":
            (ax, ay), (bx, by) = _xy(g, "start") or (0, 0), _xy(g, "end") or (0, 0)
            pts = [(ax, ay), (bx, ay), (bx, by), (ax, by)]
        elif tag == "gr_circle":
            c, e = _xy(g, "center"), _xy(g, "end")
            pts = _circle(c[0], c[1], math.hypot(e[0] - c[0], e[1] - c[1])) if c and e else []
        elif tag in ("gr_line", "gr_arc"):
            pts = [p for p in (_xy(g, "start"), _xy(g, "mid"), _xy(g, "end")) if p]
        else:
            continue
        if pts:
            out.append((pts, half))
    return out


def _pads(fp: list, side: str) -> list[tuple[list[tuple[float, float]], float]]:
    """Every copper pad on ``side`` (or through), as (polygon, half stroke) in footprint coordinates."""
    out = []
    for pad in sexpr.find_all(fp, "pad"):
        layers = _layer_names(pad)
        if not any(l in (f"{side}.Cu", "*.Cu") for l in layers):
            continue
        at, size = sexpr.find(pad, "at"), sexpr.find(pad, "size")
        if at is None or size is None:
            continue
        px, py, ang = _num(at, 1), _num(at, 2), _num(at, 3)
        hw, hh = _num(size, 1) / 2, _num(size, 2) / 2
        shapes = [([(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)], 0.0), *_primitives(pad)]
        for pts, half in shapes:
            out.append(([(px + rx, py + ry) for rx, ry in (_rotate(x, y, ang) for x, y in pts)], half))
    return out


def _inside(x: float, y: float, poly) -> bool:
    hit = False
    for i in range(len(poly)):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % len(poly)]
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            hit = not hit
    return hit


def _near(x: float, y: float, poly, d: float) -> bool:
    for i in range(len(poly)):
        (ax, ay), (bx, by) = poly[i], poly[(i + 1) % len(poly)]
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy))) if dx or dy else 0.0
        if math.hypot(x - ax - t * dx, y - ay - t * dy) <= d:
            return True
    return False


def _on_pad(x: float, y: float, pads, grow: float) -> bool:
    return any((len(poly) > 2 and _inside(x, y, poly)) or _near(x, y, poly, grow + half) for poly, half in pads)


def _stroke(node) -> float:
    stroke = sexpr.find(node, "stroke")
    w = sexpr.find(stroke, "width") if stroke is not None else sexpr.find(node, "width")
    return _num(w, 1, 0.12) if w is not None else 0.12


def _xy(node, tag: str) -> tuple[float, float] | None:
    n = sexpr.find(node, tag)
    return (_num(n, 1), _num(n, 2)) if n is not None else None


def _arc_points(node) -> list[tuple[float, float]]:
    s, m, e = _xy(node, "start"), _xy(node, "mid"), _xy(node, "end")
    if not (s and m and e):
        return [p for p in (s, m, e) if p]
    (ax, ay), (bx, by), (cx, cy) = s, m, e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [s, m, e]
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    if r <= 0:
        return [s, m, e]
    # Only the sweep from start through mid to end: the whole circumcircle
    # deleted a clear arc whenever a pad sat near the part it never draws
    # (Codex, PR #64).
    t0, tm, t1 = (math.atan2(py - uy, px - ux) for px, py in (s, m, e))
    sweep = (t1 - t0) % (2 * math.pi)
    if (tm - t0) % (2 * math.pi) > sweep:
        sweep -= 2 * math.pi
    n = max(2, int(abs(sweep) * r / _STEP))
    return [(ux + r * math.cos(t0 + sweep * k / n), uy + r * math.sin(t0 + sweep * k / n)) for k in range(n + 1)]


def _shape_points(node) -> list[tuple[float, float]]:
    """Points along a non-line graphic, for an all-or-nothing hit test."""
    tag = sexpr.head(node)
    if tag == "fp_arc":
        return _arc_points(node)
    if tag == "fp_circle":
        c, e = _xy(node, "center"), _xy(node, "end")
        if not (c and e):
            return []
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        return [(c[0] + r * math.cos(t), c[1] + r * math.sin(t)) for t in
                (2 * math.pi * k / 72 for k in range(72))]
    if tag == "fp_rect":
        s, e = _xy(node, "start"), _xy(node, "end")
        if not (s and e):
            return []
        corners = [s, (e[0], s[1]), e, (s[0], e[1])]
        return [p for i in range(4) for p in _along(corners[i], corners[(i + 1) % 4])]
    pts = [(_num(xy, 1), _num(xy, 2)) for g in sexpr.find_all(node, "pts") for xy in sexpr.find_all(g, "xy")]
    return [p for i in range(len(pts)) for p in _along(pts[i], pts[(i + 1) % len(pts)])]


def _along(a, b) -> list[tuple[float, float]]:
    n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / _STEP))
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def _trim_line(node, pads, grow: float) -> list[list] | None:
    """The pieces of an fp_line that stay clear of the pads; None when untouched."""
    s, e = _xy(node, "start"), _xy(node, "end")
    if not (s and e):
        return None
    pts = _along(s, e)
    clear = [not _on_pad(x, y, pads, grow) for x, y in pts]
    if all(clear):
        return None
    pieces, run = [], []
    for p, ok in zip(pts, clear):
        if ok:
            run.append(p)
        elif run:
            pieces.append(run)
            run = []
    if run:
        pieces.append(run)
    out = []
    for run in pieces:
        a, b = run[0], run[-1]
        if math.hypot(b[0] - a[0], b[1] - a[1]) < _MIN_KEEP:
            continue
        piece = [list(c) if isinstance(c, list) else c for c in node]
        for i, c in enumerate(piece):
            if isinstance(c, list) and sexpr.head(c) == "start":
                piece[i] = ["start", f"{a[0]:.4f}", f"{a[1]:.4f}"]
            elif isinstance(c, list) and sexpr.head(c) == "end":
                piece[i] = ["end", f"{b[0]:.4f}", f"{b[1]:.4f}"]
            elif isinstance(c, list) and sexpr.head(c) == "uuid":
                piece[i] = ["uuid", sexpr.quote(ids.new())]
        out.append(piece)
    return out


def _filled(node) -> bool:
    fill = sexpr.find(node, "fill")
    return fill is not None and len(fill) > 1 and sexpr.unquote(str(fill[1])) in ("yes", "solid")


def _hits(node, pads, grow: float) -> bool:
    """Whether a non-line graphic touches a pad, its filled interior included.

    A solid shape covering a pad has its outline clear of it: tested by its
    perimeter alone, it stayed printed over the copper (Codex, PR #64).
    """
    outline = _shape_points(node)
    if any(_on_pad(x, y, pads, grow) for x, y in outline):
        return True
    if _filled(node) and sexpr.head(node) != "fp_arc" and len(outline) > 2:
        return any(_inside(x, y, outline) for poly, _ in pads for x, y in poly)
    return False


def clip_to_copper(fp: list) -> int:
    """Trim this footprint's silk graphics off its own pads; how many changed."""
    changed = 0
    i = 0
    while i < len(fp):
        node = fp[i]
        side = _silk_side(node) if isinstance(node, list) and sexpr.head(node) in _GRAPHICS else None
        if side is None:
            i += 1
            continue
        pads = _pads(fp, side)
        grow = PAD_MARGIN + _stroke(node) / 2
        if sexpr.head(node) == "fp_line":
            pieces = _trim_line(node, pads, grow)
            if pieces is not None:
                fp[i:i + 1] = pieces
                changed += 1
                i += len(pieces)
                continue
        elif _hits(node, pads, grow):
            del fp[i]
            changed += 1
            continue
        i += 1
    return changed


def _hide(prop) -> None:
    for node in sexpr.find_all(prop, "hide"):
        node[:] = ["hide", "yes"]
        return
    prop.append(["hide", "yes"])


def apply(fp: list, *, policy: dict | None, label: str | None) -> None:
    """Apply project.yaml's silkscreen policy to one library-local footprint."""
    policy = policy or {}
    hidden = policy.get("references") == "hidden"
    size = max(MIN_TEXT, float(policy.get("label_size") or MIN_TEXT))
    anchor = None
    for prop in sexpr.find_all(fp, "property"):
        name = sexpr.unquote(str(prop[1])) if len(prop) > 1 else ""
        if name not in ("Reference", "Value") or _silk_side(prop) is None:
            continue
        if name == "Reference":
            anchor = (sexpr.find(prop, "at"), _silk_side(prop))
        # The policy hides reference designators; a Value someone put in silk
        # stays (Codex, PR #64).
        if name == "Reference" and (hidden or label):
            _hide(prop)
    if label:
        at, side = anchor if anchor and anchor[0] is not None else (["at", "0", "0"], "F")
        layer = "F.SilkS" if side == "F" else "B.SilkS"
        effects = ["effects", ["font", ["size", f"{size:g}", f"{size:g}"], ["thickness", f"{max(0.1, size * 0.15):.3g}"]]]
        if side == "B":
            effects.append(["justify", "mirror"])
        fp.append(["fp_text", "user", sexpr.quote(label), [*at], ["layer", sexpr.quote(layer)],
                   ["uuid", sexpr.quote(ids.new())], effects])
    if policy.get("clip_to_copper"):
        clip_to_copper(fp)
