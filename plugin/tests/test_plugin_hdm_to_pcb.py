"""Plugin-side tests.

Two tiers:
  1. Unit tests that inject a fake ``pcbnew`` module and assert we call the
     right APIs in the right order. Run in the standard venv — no KiCad needed.
  2. An integration smoke test that invokes KiCad's bundled Python to actually
     build a board and load it back via ``kicad-cli``. Skipped when neither
     KiCad's Python nor ``kicad-cli`` is on the system (e.g. CI without KiCad).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


_PLUGINS_ROOT = Path(__file__).resolve().parent.parent
# The unit tests only use this as a lookup key for the fake pcbnew; the
# integration test needs real footprints. The plugin vendors no library of its
# own, so take KiCad's stock footprints from BLPL_KICAD_FOOTPRINTS, or from the
# pipeline's submodule when this sits beside core/ in the umbrella checkout.
_FOOTPRINTS_ROOT = Path(
    os.environ.get("BLPL_KICAD_FOOTPRINTS") or _PLUGINS_ROOT.parent / "core" / "kicad-footprints"
)


# Make the plugin package importable in-venv for unit tests.
sys.path.insert(0, str(_PLUGINS_ROOT))

from blpl_kicad_plugin import hdm_to_pcb  # noqa: E402


# ---------------------------------------------------------------------------
# Fake pcbnew for unit tests
# ---------------------------------------------------------------------------


class _FakePad:
    def __init__(self, number: str) -> None:
        self._number = number
        self.net = None

    def GetNumber(self) -> str:
        return self._number

    def SetNet(self, net) -> None:
        self.net = net


class _FakeFootprint:
    def __init__(self, lib_ref: str) -> None:
        self.lib_ref = lib_ref
        self.position = None
        self.orientation = 0.0
        self.reference = ""
        self.value = ""
        self.flipped = False
        self._pads = [_FakePad("1"), _FakePad("2")]

    def SetPosition(self, v) -> None:
        self.position = v

    def SetOrientationDegrees(self, deg: float) -> None:
        self.orientation = deg

    def SetReference(self, r: str) -> None:
        self.reference = r

    def SetValue(self, v: str) -> None:
        self.value = v

    def Flip(self, _anchor, _aspect) -> None:
        self.flipped = True

    def Pads(self):
        return self._pads


class _FakeShape:
    def __init__(self, _board) -> None:
        self.shape = None
        self.start = None
        self.end = None
        self.layer = None
        self.width = None

    def SetShape(self, s) -> None:
        self.shape = s

    def SetStart(self, v) -> None:
        self.start = v

    def SetEnd(self, v) -> None:
        self.end = v

    def SetLayer(self, layer) -> None:
        self.layer = layer

    def SetWidth(self, w) -> None:
        self.width = w

    def SetArcGeometry(self, start, mid, end) -> None:
        self.start, self.mid, self.end = start, mid, end

    def SetCenter(self, v) -> None:
        self.center = v


# Layer numbers as a four-layer board's GetLayerID would give them.
_LAYER_IDS = {"F.Cu": 0, "In1.Cu": 4, "In2.Cu": 6, "B.Cu": 2, "F.SilkS": 9}
_ALL_CU_IDS = {0, 4, 6, 2}


class _FakeLayerSet:

    def __init__(self) -> None:
        self.layers: set = set()

    @classmethod
    def AllCuMask(cls) -> "_FakeLayerSet":
        out = cls()
        out.layers = set(_ALL_CU_IDS)
        return out

    def AddLayer(self, layer) -> None:
        self.layers.add(layer)

    def AddLayerSet(self, other) -> None:
        self.layers |= other.layers


class _FakeOutline:
    def __init__(self) -> None:
        self.outlines: list[list] = []

    def NewOutline(self) -> None:
        self.outlines.append([])

    def Append(self, x: int, y: int) -> None:
        self.outlines[-1].append((x, y))


class _FakeZone:
    def __init__(self, _board) -> None:
        self.rule_area = False
        self.layer_set = None
        self.forbids: dict[str, bool] = {}
        self.name = ""
        self._outline = _FakeOutline()

    def SetIsRuleArea(self, v: bool) -> None:
        self.rule_area = v

    def SetLayerSet(self, lset) -> None:
        self.layer_set = lset

    def SetDoNotAllowTracks(self, v: bool) -> None:
        self.forbids["tracks"] = v

    def SetDoNotAllowVias(self, v: bool) -> None:
        self.forbids["vias"] = v

    def SetDoNotAllowPads(self, v: bool) -> None:
        self.forbids["pads"] = v

    def SetDoNotAllowFootprints(self, v: bool) -> None:
        self.forbids["footprints"] = v

    def SetDoNotAllowZoneFills(self, v: bool) -> None:
        self.forbids["copperpour"] = v

    def SetZoneName(self, name: str) -> None:
        self.name = name

    def Outline(self) -> _FakeOutline:
        return self._outline


class _FakeNet:
    def __init__(self, _board, name: str) -> None:
        self.name = name


class _FakeBoard:
    def __init__(self) -> None:
        self.items: list = []
        # A fresh pcbnew BOARD has two copper layers.
        self.copper_layers = 2

    def SetCopperLayerCount(self, n: int) -> None:
        self.copper_layers = n

    def GetCopperLayerCount(self) -> int:
        return self.copper_layers

    def IsLayerEnabled(self, layer_id: int) -> bool:
        inner = {_LAYER_IDS["In1.Cu"], _LAYER_IDS["In2.Cu"]}
        return layer_id not in inner or self.copper_layers >= 4

    def Add(self, item) -> None:
        self.items.append(item)

    def GetLayerID(self, name: str) -> int:
        # Negative for a name the board does not have, as the real call does.
        return _LAYER_IDS.get(name, -1)


class _FakeVector2I:
    def __init__(self, x: int, y: int) -> None:
        self.x = x
        self.y = y

    def __repr__(self) -> str:
        return f"V2I({self.x},{self.y})"

    def __eq__(self, other) -> bool:
        return isinstance(other, _FakeVector2I) and (self.x, self.y) == (other.x, other.y)


def _make_fake_pcbnew(footprint_lookup: dict[tuple[str, str], _FakeFootprint]) -> SimpleNamespace:
    def _footprint_load(lib_path: str, name: str):
        return footprint_lookup.get((lib_path, name))

    return SimpleNamespace(
        BOARD=_FakeBoard,
        FootprintLoad=_footprint_load,
        NETINFO_ITEM=_FakeNet,
        PCB_SHAPE=_FakeShape,
        ZONE=_FakeZone,
        LSET=_FakeLayerSet,
        IsCopperLayer=lambda layer_id: layer_id in _ALL_CU_IDS,
        VECTOR2I=_FakeVector2I,
        Edge_Cuts="Edge.Cuts",
        SHAPE_T_SEGMENT="SEGMENT",
        SHAPE_T_ARC="ARC",
        SHAPE_T_CIRCLE="CIRCLE",
    )


def _minimal_hdm() -> dict:
    return {
        "project": {"name": "Plugin Test", "dimensions": [50, 40]},
        "components": {
            "J1": {
                "value": "PJ-063AH",
                "footprint": "Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal",
                "placement": {"x": 20, "y": 20, "rot": 0, "side": "top"},
                "pin_map": {"VIN": "1", "GND": "2"},
            }
        },
        "nets": {
            "VIN": {"class": "Default", "pads": [["J1", "VIN"]]},
            "GND": {"class": "Default", "pads": [["J1", "GND"]]},
        },
    }


# ---------------------------------------------------------------------------
# Unit tests against the fake pcbnew
# ---------------------------------------------------------------------------


def test_build_board_places_footprint_at_offset_origin() -> None:
    """HDM (20, 20) + working-area origin (12.5, 12.5) = sheet (32.5, 32.5)."""
    fake_fp = _FakeFootprint("Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal")
    fake_pcbnew = _make_fake_pcbnew(
        {(str(_FOOTPRINTS_ROOT / "Connector_BarrelJack.pretty"), "BarrelJack_CUI_PJ-063AH_Horizontal"): fake_fp}
    )
    board, report = hdm_to_pcb.build_board(
        _minimal_hdm(), footprints_root=_FOOTPRINTS_ROOT, pcbnew_module=fake_pcbnew
    )
    assert report.footprints_placed == 1
    assert report.footprints_missing == []
    assert fake_fp.position == _FakeVector2I(int(32.5 * 1_000_000), int(32.5 * 1_000_000))
    assert fake_fp.reference == "J1"
    assert fake_fp.value == "PJ-063AH"
    assert report.edge_cut_segments == 4
    assert report.nets_created == 2


def test_build_board_assigns_nets_to_pads_via_pin_map() -> None:
    fake_fp = _FakeFootprint("Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal")
    fake_pcbnew = _make_fake_pcbnew(
        {(str(_FOOTPRINTS_ROOT / "Connector_BarrelJack.pretty"), "BarrelJack_CUI_PJ-063AH_Horizontal"): fake_fp}
    )
    hdm_to_pcb.build_board(
        _minimal_hdm(), footprints_root=_FOOTPRINTS_ROOT, pcbnew_module=fake_pcbnew
    )
    pad_nets = {p.GetNumber(): (p.net.name if p.net else None) for p in fake_fp.Pads()}
    assert pad_nets == {"1": "VIN", "2": "GND"}


def test_build_board_reports_missing_footprint() -> None:
    hdm = _minimal_hdm()
    hdm["components"]["J_MISSING"] = {
        "value": "?",
        "footprint": "No_Such_Lib:NothingHere",
        "placement": {"x": 0, "y": 0},
    }
    fake_fp = _FakeFootprint("Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal")
    fake_pcbnew = _make_fake_pcbnew(
        {(str(_FOOTPRINTS_ROOT / "Connector_BarrelJack.pretty"), "BarrelJack_CUI_PJ-063AH_Horizontal"): fake_fp}
    )
    _board, report = hdm_to_pcb.build_board(
        hdm, footprints_root=_FOOTPRINTS_ROOT, pcbnew_module=fake_pcbnew
    )
    assert report.footprints_placed == 1
    assert report.footprints_missing == ["J_MISSING"]


def test_build_board_emits_edge_cuts_rectangle_at_board_dimensions() -> None:
    fake_fp = _FakeFootprint("Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal")
    fake_pcbnew = _make_fake_pcbnew(
        {(str(_FOOTPRINTS_ROOT / "Connector_BarrelJack.pretty"), "BarrelJack_CUI_PJ-063AH_Horizontal"): fake_fp}
    )
    board, _report = hdm_to_pcb.build_board(
        _minimal_hdm(), footprints_root=_FOOTPRINTS_ROOT, pcbnew_module=fake_pcbnew
    )
    shapes = [it for it in board.items if isinstance(it, _FakeShape)]
    assert len(shapes) == 4
    # Starts at (12.5, 12.5) in nm; board is 50×40 → ends at (62.5, 52.5).
    corners = {(s.start.x, s.start.y) for s in shapes} | {(s.end.x, s.end.y) for s in shapes}
    expected = {
        (int(12.5 * 1_000_000), int(12.5 * 1_000_000)),
        (int(62.5 * 1_000_000), int(12.5 * 1_000_000)),
        (int(62.5 * 1_000_000), int(52.5 * 1_000_000)),
        (int(12.5 * 1_000_000), int(52.5 * 1_000_000)),
    }
    assert expected.issubset(corners)
    assert all(s.layer == "Edge.Cuts" for s in shapes)


def test_build_board_honours_bottom_side_placement_by_flipping() -> None:
    hdm = _minimal_hdm()
    hdm["components"]["J1"]["placement"]["side"] = "bottom"
    fake_fp = _FakeFootprint("Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal")
    fake_pcbnew = _make_fake_pcbnew(
        {(str(_FOOTPRINTS_ROOT / "Connector_BarrelJack.pretty"), "BarrelJack_CUI_PJ-063AH_Horizontal"): fake_fp}
    )
    hdm_to_pcb.build_board(
        hdm, footprints_root=_FOOTPRINTS_ROOT, pcbnew_module=fake_pcbnew
    )
    assert fake_fp.flipped is True


def _nm(x_mm: float, y_mm: float) -> _FakeVector2I:
    """Board-local mm to the sheet-absolute nm the plugin draws at."""
    return _FakeVector2I(round((x_mm + 12.5) * 1_000_000), round((y_mm + 12.5) * 1_000_000))


def _shaped_hdm() -> dict:
    """A 40×30 board with two rounded corners, a round cutout and an antenna keepout.

    Shaped the way core Stage 5 writes ``board_geometry`` (``as_record``):
    board-local mm, arcs as start/mid/end, a circle as centre plus radius.
    """
    hdm = _minimal_hdm()
    hdm["project"]["dimensions"] = [40, 30]
    hdm["board_geometry"] = {
        "outline": {
            "declared": True,
            "segments": [
                {"kind": "line", "points": [[2, 0], [38, 0]]},
                {"kind": "arc", "points": [[38, 0], [39.414214, 0.585786], [40, 2]]},
                {"kind": "line", "points": [[40, 2], [40, 30]]},
                {"kind": "line", "points": [[40, 30], [0, 30]]},
                {"kind": "line", "points": [[0, 30], [0, 2]]},
                {"kind": "arc", "points": [[0, 2], [0.585786, 0.585786], [2, 0]]},
            ],
            "polygon": [[2, 0], [38, 0], [40, 2], [40, 30], [0, 30], [0, 2]],
        },
        "features": [
            {
                "kind": "cutout",
                "where": "boundaries.cutouts[0]",
                "polygon": [[11.5, 10], [10, 11.5], [8.5, 10], [10, 8.5]],
                "segments": [{"kind": "circle", "points": [[10, 10]], "radius": 1.5}],
            },
            {
                "kind": "keepout",
                "where": "boundaries.keepouts[0]",
                "name": "ANT",
                "anchor": "J1",
                "layers": ["*.Cu"],
                "forbid": ["tracks", "copperpour"],
                "polygon": [[20, 5], [30, 5], [30, 15], [20, 15]],
                "segments": [],
            },
        ],
    }
    return hdm


def _build(hdm: dict):
    return hdm_to_pcb.build_board(
        hdm, footprints_root=_FOOTPRINTS_ROOT, pcbnew_module=_make_fake_pcbnew({})
    )


def test_board_geometry_outline_keeps_arcs_as_arcs() -> None:
    board, report = _build(_shaped_hdm())
    shapes = [it for it in board.items if isinstance(it, _FakeShape)]
    # Six outline segments plus the cutout's one circle.
    assert report.edge_cut_segments == 7 == len(shapes)
    assert [s.shape for s in shapes[:6]] == ["SEGMENT", "ARC", "SEGMENT", "SEGMENT", "SEGMENT", "ARC"]
    arc = shapes[1]
    assert (arc.start, arc.mid, arc.end) == (_nm(38, 0), _nm(39.414214, 0.585786), _nm(40, 2))
    assert all(s.layer == "Edge.Cuts" for s in shapes)
    assert all(s.width == 50_000 for s in shapes)


def test_board_geometry_outline_is_closed() -> None:
    """Each segment starts where the last ended — an open outline fails DRC."""
    board, _report = _build(_shaped_hdm())
    outline = [it for it in board.items if isinstance(it, _FakeShape)][:6]
    for prev, cur in zip(outline, outline[1:] + outline[:1]):
        assert prev.end == cur.start


def test_board_geometry_cutout_circle_is_centre_and_radius() -> None:
    board, _report = _build(_shaped_hdm())
    circle = [it for it in board.items if isinstance(it, _FakeShape)][6]
    assert circle.shape == "CIRCLE"
    assert circle.center == _nm(10, 10)
    assert circle.end == _nm(11.5, 10)


def test_board_geometry_keepout_becomes_rule_area_with_its_forbids() -> None:
    board, report = _build(_shaped_hdm())
    zones = [it for it in board.items if isinstance(it, _FakeZone)]
    assert report.keepout_zones == 1 == len(zones)
    zone = zones[0]
    assert zone.rule_area is True
    assert zone.name == "ANT"
    assert zone.forbids == {
        "tracks": True, "vias": False, "pads": False, "footprints": False, "copperpour": True,
    }
    assert zone.layer_set.layers == _ALL_CU_IDS
    assert zone.Outline().outlines == [
        [(p.x, p.y) for p in (_nm(20, 5), _nm(30, 5), _nm(30, 15), _nm(20, 15))]
    ]


def test_board_geometry_keepout_on_named_layers() -> None:
    hdm = _shaped_hdm()
    keepout = hdm["board_geometry"]["features"][1]
    keepout["layers"] = ["F&B.Cu", "F.SilkS"]
    board, _report = _build(hdm)
    zone = next(it for it in board.items if isinstance(it, _FakeZone))
    assert zone.layer_set.layers == {_LAYER_IDS[n] for n in ("F.Cu", "B.Cu", "F.SilkS")}


def test_board_geometry_keepout_on_unknown_layer_is_refused() -> None:
    """Dropping the layer would draw a keepout that guards less than asked."""
    hdm = _shaped_hdm()
    hdm["board_geometry"]["features"][1]["layers"] = ["In7.Cu"]
    with pytest.raises(ValueError, match="In7.Cu"):
        _build(hdm)


def test_board_geometry_unknown_segment_kind_is_refused() -> None:
    hdm = _shaped_hdm()
    hdm["board_geometry"]["outline"]["segments"][0]["kind"] = "bezier"
    with pytest.raises(ValueError, match="bezier"):
        _build(hdm)


def test_board_geometry_inner_layer_keepout_refused_on_two_layer_board() -> None:
    """GetLayerID resolves In1.Cu on a two-layer board; the rule would guard nothing."""
    hdm = _shaped_hdm()
    hdm["board_geometry"]["features"][1]["layers"] = ["In1.Cu"]
    with pytest.raises(ValueError, match="In1.Cu.*2-layer"):
        _build(hdm)


def test_board_geometry_inner_layer_keepout_follows_hdm_stackup() -> None:
    hdm = _shaped_hdm()
    hdm["project"]["stackup"] = {"layers": 4, "thickness": 1.6}
    hdm["board_geometry"]["features"][1]["layers"] = ["In1.Cu"]
    board, report = _build(hdm)
    assert board.copper_layers == 4
    assert report.keepout_zones == 1
    zone = next(it for it in board.items if isinstance(it, _FakeZone))
    assert zone.layer_set.layers == {_LAYER_IDS["In1.Cu"]}


def test_board_geometry_refusal_adds_nothing_to_the_board() -> None:
    """A late failure must not leave the outline's first segments on an open board."""
    hdm = _shaped_hdm()
    hdm["board_geometry"]["features"][1]["layers"] = ["In7.Cu"]
    board = _FakeBoard()
    with pytest.raises(ValueError):
        hdm_to_pcb._add_board_geometry(board, hdm, _make_fake_pcbnew({}))
    assert board.items == []


def test_board_geometry_absent_falls_back_to_dimensions_rectangle() -> None:
    board, report = _build(_minimal_hdm())
    assert report.edge_cut_segments == 4
    assert report.keepout_zones == 0
    assert not [it for it in board.items if isinstance(it, _FakeZone)]


# ---------------------------------------------------------------------------
# Integration smoke — only when KiCad is available
# ---------------------------------------------------------------------------


def _find_kicad_python() -> Path | None:
    candidates = [
        "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
        "/usr/lib/kicad/bin/python3",
    ]
    if envval := os.environ.get("HDM_KICAD_PYTHON"):
        candidates.insert(0, envval)
    for c in candidates:
        if Path(c).exists():
            return Path(c)
    which = shutil.which("kicad-python")
    return Path(which) if which else None


@pytest.mark.skipif(
    _find_kicad_python() is None
    or shutil.which("kicad-cli") is None
    or not _FOOTPRINTS_ROOT.is_dir(),
    reason="KiCad's bundled Python, kicad-cli or the stock footprints are not available.",
)
def test_plugin_produces_kicad_cli_loadable_board(tmp_path: Path) -> None:
    import json

    # KiCad's bundled Python lacks pyyaml — pass JSON so the plugin reads stdlib-only.
    hdm_path = tmp_path / "hdm.json"
    hdm_path.write_text(json.dumps(_minimal_hdm()))
    out_path = tmp_path / "plugin_build.kicad_pcb"

    kicad_python = _find_kicad_python()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_PLUGINS_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    cmd = [
        str(kicad_python),
        "-m",
        "blpl_kicad_plugin.build_pcb",
        "--hdm",
        str(hdm_path),
        "--out",
        str(out_path),
        "--footprints-root",
        str(_FOOTPRINTS_ROOT),
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"build_pcb failed: {proc.stderr}"
    assert out_path.exists() and out_path.stat().st_size > 0

    # Verify kicad-cli can actually load the file.
    drc_report = tmp_path / "drc.json"
    drc = subprocess.run(
        ["kicad-cli", "pcb", "drc", "--output", str(drc_report), "--format", "json", str(out_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # DRC may report violations (design isn't routed) — non-zero exit is fine
    # as long as the file loaded (which is implied by a report being written).
    assert drc_report.exists(), f"kicad-cli couldn't load the board: {drc.stderr}"
