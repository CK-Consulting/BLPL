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
_FOOTPRINTS_ROOT = Path(__file__).resolve().parent.parent / "kicad-footprints"


# Make the plugin package importable in-venv for unit tests.
sys.path.insert(0, str(_PLUGINS_ROOT))

from blpl.plugin_kicad import hdm_to_pcb  # noqa: E402


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


class _FakeNet:
    def __init__(self, _board, name: str) -> None:
        self.name = name


class _FakeBoard:
    def __init__(self) -> None:
        self.items: list = []

    def Add(self, item) -> None:
        self.items.append(item)


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
        VECTOR2I=_FakeVector2I,
        Edge_Cuts="Edge.Cuts",
        SHAPE_T_SEGMENT="SEGMENT",
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
    _find_kicad_python() is None or shutil.which("kicad-cli") is None,
    reason="KiCad's bundled Python or kicad-cli not available on this system.",
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
        "blpl.plugin_kicad.build_pcb",
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
