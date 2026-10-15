"""Bottom-side and rotated footprints, checked against what KiCad itself writes.

The emitter used to relabel a bottom-side footprint ``(layer "B.Cu")`` and do
nothing else: its pads stayed on F.Cu/F.Mask/F.Paste at their unmirrored
coordinates, a front-side land pattern under a back-side label. A rotated part
on either side kept its pads at their library angle, which KiCad reads as
absolute, so they loaded turned against their own footprint.

The oracle is ``fixtures/footprint_flip/kicad_placed.kicad_pcb``: a footprint
authored for this test, placed and flipped by pcbnew (``regenerate.py`` there
says how), compared here item by item with what the emitter writes for the
same placements.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from blpl.core import board_geometry as bg
from blpl.core import placement, stage5_emit_yaml_hdm as s5
from blpl.emitter import pcb, sexpr

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "footprint_flip"
_STOCK = Path(__file__).resolve().parent.parent / "kicad-footprints"

#: Must match regenerate.py's PLACEMENTS.
_PLACEMENTS = [
    ("U1", 10, 10, 0, "bottom"),
    ("U2", 30, 10, 30, "bottom"),
    ("U3", 50, 10, 200, "bottom"),
    ("U4", 70, 10, 30, "top"),
]


def _hdm(placements, footprint="BLPL_Test:FlipProbe") -> dict:
    return {
        "project": {"dimensions": [100, 40]},
        "components": {
            ref: {"footprint": footprint, "value": "FlipProbe",
                  "placement": {"x": x, "y": y, "rot": rot, "side": side}}
            for ref, x, y, rot, side in placements
        },
        "nets": {},
    }


def _footprints(board: sexpr.Node) -> dict[str, sexpr.Node]:
    out = {}
    for fp in sexpr.find_all(board, "footprint"):
        for prop in sexpr.find_all(fp, "property"):
            if sexpr.unquote(prop[1]) == "Reference":
                out[sexpr.unquote(prop[2])] = fp
    return out


def _pt(node: sexpr.Node) -> tuple[float, float]:
    return (round(float(node[1]), 3) + 0.0, round(float(node[2]), 3) + 0.0)


def _angle(at: sexpr.Node | None) -> float:
    if at is None or len(at) < 4:
        return 0.0
    return round(float(at[3]) % 360.0, 3) % 360.0


def _layers(node: sexpr.Node) -> tuple[str, ...]:
    return tuple(sorted(sexpr.unquote(a) for tag in ("layer", "layers")
                        for n in sexpr.find_all(node, tag) for a in n[1:]))


def _xys(node) -> list[tuple[float, float]]:
    out = []
    for child in node:
        if isinstance(child, list) and child:
            if child[0] == "xy":
                out.append(_pt(child))
            else:
                out.extend(_xys(child))
    return out


def _canon(fp: sexpr.Node) -> dict:
    """What a footprint *means* on the board, without KiCad's spelling of it.

    KiCad saves a rect turned off-axis as a polygon, recomputes an arc's mid
    point, may swap an arc's ends and writes ``-20`` for ``340``; none of that
    is a difference.
    """
    at = sexpr.find(fp, "at")
    pads, texts, shapes = {}, {}, []
    for pad in sexpr.find_all(fp, "pad"):
        chamfer = sexpr.find(pad, "chamfer")
        delta = sexpr.find(pad, "rect_delta")
        pads[sexpr.unquote(pad[1])] = (
            _pt(sexpr.find(pad, "at")), _angle(sexpr.find(pad, "at")), _layers(pad),
            tuple(sorted(chamfer[1:])) if chamfer else (),
            _pt(delta) if delta else None,
            tuple(_xys(sexpr.find(pad, "primitives") or [])),
        )
    for tag in ("property", "fp_text"):
        for t in sexpr.find_all(fp, tag):
            key = sexpr.unquote(t[1] if tag == "property" else t[2])
            effects = sexpr.find(t, "effects")
            justify = sexpr.find(effects, "justify") if effects else None
            texts[key] = (_pt(sexpr.find(t, "at")), _angle(sexpr.find(t, "at")),
                          _layers(t), tuple(sorted(justify[1:])) if justify else ())
    for tag in ("fp_line", "fp_circle", "fp_arc"):
        for s in sexpr.find_all(fp, tag):
            ends = ("center", "end") if tag == "fp_circle" else ("start", "end")
            pts = tuple(_pt(sexpr.find(s, e)) for e in ends)
            # KiCad stores an arc one way round and may swap its ends to do it.
            shapes.append((tag, _layers(s), tuple(sorted(pts)) if tag == "fp_arc" else pts))
    for s in sexpr.find_all(fp, "fp_poly"):
        shapes.append(("poly", _layers(s), tuple(sorted(_xys(s)))))
    for s in sexpr.find_all(fp, "fp_rect"):
        (x0, y0), (x1, y1) = _pt(sexpr.find(s, "start")), _pt(sexpr.find(s, "end"))
        shapes.append(("poly", _layers(s), tuple(sorted({(x0, y0), (x1, y0), (x1, y1), (x0, y1)}))))
    zones = sorted((_layers(z), tuple(sorted(_xys(sexpr.find(z, "polygon")))))
                   for z in sexpr.find_all(fp, "zone"))
    return {"layer": _layers(fp)[0], "at": (_pt(at), _angle(at)), "pads": pads,
            "texts": texts, "shapes": sorted(shapes), "zones": zones}


@pytest.fixture(scope="module")
def placed() -> tuple[dict, dict]:
    kicad = _footprints(sexpr.parse((_FIXTURES / "kicad_placed.kicad_pcb").read_text()))
    ours = _footprints(sexpr.parse(pcb.emit(_hdm(_PLACEMENTS), footprints_root=[_FIXTURES])))
    return kicad, ours


@pytest.mark.parametrize("ref,rot,side", [(r, rot, side) for r, _, _, rot, side in _PLACEMENTS])
def test_the_emitter_places_a_footprint_the_way_kicad_does(placed, ref, rot, side):
    kicad, ours = placed
    want, got = _canon(kicad[ref]), _canon(ours[ref])
    assert got["layer"] == want["layer"]
    assert got["at"] == want["at"]
    assert got["pads"] == want["pads"]
    assert got["shapes"] == want["shapes"]
    assert got["zones"] == want["zones"]
    # KiCad also writes the Datasheet and Description fields; every text the
    # emitter writes must match KiCad's.
    assert {k: want["texts"][k] for k in got["texts"]} == got["texts"]


def test_a_bottom_side_part_has_its_copper_on_the_back():
    """The reported bug, on a stock 0603: the footprint said B.Cu and every
    pad still said F.Cu/F.Mask/F.Paste."""
    hdm = _hdm([("R1", 20, 20, 0, "bottom")], footprint="Resistor_SMD:R_0603_1608Metric")
    fp = _footprints(sexpr.parse(pcb.emit(hdm, footprints_root=_STOCK)))["R1"]
    assert sexpr.unquote(sexpr.find(fp, "layer")[1]) == "B.Cu"
    for pad in sexpr.find_all(fp, "pad"):
        assert _layers(pad) == ("B.Cu", "B.Mask", "B.Paste")
    front = [n for tag in ("fp_line", "fp_rect", "fp_text", "property")
             for n in sexpr.find_all(fp, tag) if any(l.startswith("F.") for l in _layers(n))]
    assert front == []


def test_a_rotated_part_turns_its_pads_with_it():
    """KiCad reads a pad's angle as absolute. An 0603 at 90 deg written with its
    library angle of 0 loaded with each pad turned -90 deg against the part."""
    hdm = _hdm([("R1", 20, 20, 90, "top")], footprint="Resistor_SMD:R_0603_1608Metric")
    fp = _footprints(sexpr.parse(pcb.emit(hdm, footprints_root=_STOCK)))["R1"]
    assert {_angle(sexpr.find(p, "at")) for p in sexpr.find_all(fp, "pad")} == {90.0}


def test_the_footprint_position_comes_before_its_children():
    """KiCad applies (at ...) when its parser reaches it, to whatever it has
    already read. After the pads, it turns them a second time."""
    fp = _footprints(sexpr.parse(pcb.emit(_hdm(_PLACEMENTS[1:2]), footprints_root=[_FIXTURES])))["U2"]
    heads = [sexpr.head(c) for c in fp if isinstance(c, list)]
    assert heads.index("at") < heads.index("property") < heads.index("pad")


# --- placement agrees with the emitter -------------------------------------


def test_rotated_geometry_mirrors_a_bottom_side_offset():
    assert placement.rotated_geometry(4, 2, 1.0, 0.5, 0, "bottom") == (4, 2, -1.0, 0.5)
    # 90 deg on top sends (dx, dy) to (dy, -dx); turning it over negates x.
    assert placement.rotated_geometry(4, 2, 1.0, 0.5, 90, "bottom") == (2, 4, -0.5, -1.0)


@pytest.mark.parametrize("rot", [0, 90, 30])
def test_the_placer_reserves_the_box_the_emitter_draws(rot):
    """A bottom part's collision box must sit where its courtyard actually
    lands, not on the mirror image of it."""
    comps = {"U1": {"footprint": "BLPL_Test:FlipProbe", "value": "FlipProbe"}}
    hint = {"x": 30, "y": 20, "rot": rot, "side": "bottom"}
    r = placement.place(comps, {}, (60, 40), hints={"U1": hint},
                        footprint_roots=[_FIXTURES])
    box = placement.occupied_box(r, "U1")

    comps["U1"]["placement"] = r.placements["U1"].as_dict()
    hdm = {"project": {"dimensions": [60, 40]}, "components": comps, "nets": {}}
    fp = _footprints(sexpr.parse(pcb.emit(hdm, footprints_root=[_FIXTURES])))["U1"]
    at = sexpr.find(fp, "at")
    ox, oy, o = float(at[1]) - 12.5, float(at[2]) - 12.5, _angle(at)
    rect = next(s for s in sexpr.find_all(fp, "fp_rect") if _layers(s) == ("B.CrtYd",))
    import math
    c, sn = math.cos(math.radians(o)), math.sin(math.radians(o))
    corners = []
    (x0, y0), (x1, y1) = _pt(sexpr.find(rect, "start")), _pt(sexpr.find(rect, "end"))
    for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        corners.append((ox + px * c + py * sn, oy - px * sn + py * c))
    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
    assert box == pytest.approx((min(xs), min(ys), max(xs), max(ys)), abs=1e-3)


# --- anchored shapes ---------------------------------------------------------


def _board(**boundaries) -> bg.Board:
    return bg.from_config({"project": {"dimensions": [50, 30]}, "boundaries": boundaries})


def test_a_shape_anchored_to_a_bottom_side_part_is_mirrored_with_it():
    """It used to be refused, because the footprint itself was not mirrored."""
    board = _board(keepouts=[{"anchor": "U1", "shape": "rect", "center": [3, 1],
                              "size": [2, 2]}])
    (top,) = bg.place_anchored(board, "U1", 20, 15, 0, "top")
    (bottom,) = bg.place_anchored(board, "U1", 20, 15, 0, "bottom")
    assert top.shape.bbox == pytest.approx((22, 15, 24, 17))
    assert bottom.shape.bbox == pytest.approx((16, 15, 18, 17))
    # Turned first, then over: (3, 1) at 90 deg is (1, -3), mirrored (-1, -3).
    (turned,) = bg.place_anchored(_board(keepouts=[{"anchor": "U1", "shape": "rect",
                                                    "center": [3, 1], "size": [2, 2]}]),
                                  "U1", 20, 15, 90, "bottom")
    assert turned.shape.bbox == pytest.approx((18, 11, 20, 13))


def test_an_anchored_keepout_lands_where_the_bottom_side_footprint_puts_its_zone():
    """The probe footprint's own keepout zone and a project.yaml keepout
    anchored to the same spot must coincide once both are turned over."""
    zone = [(2, -2), (3, -2), (3, -1.6), (2, -1.6)]  # FlipProbe's zone, footprint frame
    board = _board(keepouts=[{"anchor": "U1", "shape": "polygon", "points": zone}])
    (placed,) = bg.place_anchored(board, "U1", 30, 10, 30, "bottom")
    fp = _footprints(sexpr.parse(pcb.emit(_hdm(_PLACEMENTS[1:2]), footprints_root=[_FIXTURES])))["U2"]
    emitted = sorted((x - 12.5, y - 12.5) for x, y in _xys(sexpr.find(sexpr.find(fp, "zone"), "polygon")))
    flat = [v for p in sorted(placed.shape.polygon) for v in p]
    assert flat == pytest.approx([v for p in emitted for v in p], abs=1e-3)


def test_stage5_accepts_a_shape_anchored_to_a_bottom_side_part(tmp_path):
    cfg = {"project": {"name": "p", "dimensions": [50, 30]},
           "boundaries": {"keepouts": [{"anchor": "R0", "shape": "rect", "center": [3, 0],
                                        "size": [2, 2]}]},
           "test_points": {"policy": "none"},
           "placement": {"hints": {"R0": {"x": 20, "y": 15, "side": "bottom"}}}}
    path = tmp_path / "project.yaml"
    path.write_text(yaml.safe_dump(cfg))
    config = s5.ensure_project_config(path, "proj")
    bom = {"project_id": "proj", "schema_version": 1, "rows": [
        {"local_id": "R0", "refdes": "R0", "mpn": "RC0402", "package": "0402",
         "footprint_hint": "Resistor_SMD:R_0402_1005Metric", "symbol_hint": "Device:R",
         "confidence": 0.9}]}
    nets = {"project_id": "proj", "schema_version": 1, "nets": []}
    da = {"project_id": "proj", "schema_version": 1, "source_files": [], "components": [],
          "connectors": [], "subsystems": [], "raw_nets": []}
    hdm, _, _ = s5.emit(bom, nets, da, config)
    (keepout,) = [f for f in hdm["board_geometry"]["features"] if f.get("anchor") == "R0"]
    xs = [p[0] for p in keepout["polygon"]]
    assert (min(xs), max(xs)) == pytest.approx((16, 18))


def test_an_anchored_keepout_turns_over_with_its_bottom_side_part_once():
    """Stage 5 places an anchored keepout already mirrored; the emitter puts it
    inside the footprint after orienting, so it must not be turned again. Its
    layers follow the part over, like the footprint's own zones."""
    board = _board(keepouts=[{"anchor": "U1", "shape": "rect", "center": [3, 1],
                              "size": [2, 2], "layers": ["F.Cu"], "name": "anchored"}])
    bg.place_anchored(board, "U1", 30, 10, 30, "bottom")
    hdm = _hdm([("U1", 30, 10, 30, "bottom")])
    hdm["board_geometry"] = bg.as_record(board)
    fp = _footprints(sexpr.parse(pcb.emit(hdm, footprints_root=[_FIXTURES])))["U1"]
    (keepout,) = [z for z in sexpr.find_all(fp, "zone")
                  if sexpr.find(z, "name") and sexpr.unquote(sexpr.find(z, "name")[1]) == "anchored"]
    assert _layers(keepout) == ("B.Cu",)
    emitted = sorted((x - 12.5, y - 12.5) for x, y in _xys(keepout))
    want = sorted(board.features[-1].shape.polygon)
    assert [v for p in emitted for v in p] == pytest.approx([v for p in want for v in p], abs=1e-3)
