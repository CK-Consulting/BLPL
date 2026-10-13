"""Reading HDM in a Python without pyyaml — KiCad's bundled one.

The ActionPlugin used to import pyyaml unconditionally, so on a stock KiCad
the Tools-menu entry failed with ModuleNotFoundError before reading anything
(Codex, PR #18). These tests block pyyaml the way KiCad's interpreter lacks it
and drive both entry points through the shared reader.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blpl_kicad_plugin import build_pcb, hdm_io  # noqa: E402

_HDM = {
    "board": {"dimensions": [20, 10]},
    "components": {},
    "nets": {},
}


@pytest.fixture
def no_pyyaml(monkeypatch):
    # A None entry makes `import yaml` raise ImportError, as it does in KiCad.
    monkeypatch.setitem(sys.modules, "yaml", None)


def test_json_reads_without_pyyaml(tmp_path: Path, no_pyyaml) -> None:
    p = tmp_path / "hdm.plugin.json"
    p.write_text(json.dumps(_HDM))
    assert hdm_io.load_hdm(p) == _HDM
    assert build_pcb._load_hdm(p) == _HDM


def test_yaml_without_pyyaml_is_refused_with_directions(tmp_path: Path, no_pyyaml) -> None:
    p = tmp_path / "hdm.yaml"
    p.write_text("components: {}\n")
    with pytest.raises(hdm_io.HdmLoadError) as e:
        hdm_io.load_hdm(p)
    assert "JSON" in str(e.value) and "stage6-plugin" in str(e.value)
    with pytest.raises(RuntimeError):
        build_pcb._load_hdm(p)


def _fake_kicad(monkeypatch, picked: Path):
    """A pcbnew and wx just real enough to run LoadHDMPlugin.Run()."""
    shown: list[str] = []
    board = SimpleNamespace(items=[], Add=lambda item: board.items.append(item))

    pcbnew = ModuleType("pcbnew")
    pcbnew.ActionPlugin = object
    pcbnew.GetBoard = lambda: board
    pcbnew.Refresh = lambda: None
    pcbnew.NETINFO_ITEM = object
    monkeypatch.setitem(sys.modules, "pcbnew", pcbnew)

    class _Dialog:
        def __init__(self, *a, **k) -> None:
            self.wildcard = k.get("wildcard", "")

        def ShowModal(self):
            return wx.ID_OK

        def GetPath(self) -> str:
            return str(picked)

        def Destroy(self) -> None:
            pass

    wx = ModuleType("wx")
    wx.ID_OK, wx.OK, wx.ICON_ERROR, wx.FD_OPEN, wx.FD_FILE_MUST_EXIST = 1, 2, 4, 8, 16
    wx.FileDialog = _Dialog
    wx.MessageBox = lambda msg, *a, **k: shown.append(msg)
    monkeypatch.setitem(sys.modules, "wx", wx)

    from blpl_kicad_plugin import hdm_to_pcb

    # Placement and outline are hdm_to_pcb's job and covered by its own tests;
    # here only the loading path matters.
    monkeypatch.setattr(hdm_to_pcb, "_add_edge_cuts_rect", lambda *a: 4)
    monkeypatch.delitem(sys.modules, "blpl_kicad_plugin.action_load_hdm", raising=False)
    from blpl_kicad_plugin import action_load_hdm

    # Bind the fakes on the module too: on a host with KiCad installed, the
    # package's __init__ has already imported the real pcbnew, and the
    # module-level name can end up pointing at it.
    monkeypatch.setattr(action_load_hdm, "pcbnew", pcbnew)
    return action_load_hdm.LoadHDMPlugin(), shown


def test_gui_loads_json_on_stock_kicad(tmp_path: Path, monkeypatch, no_pyyaml) -> None:
    p = tmp_path / "hdm.plugin.json"
    p.write_text(json.dumps(_HDM))
    plugin, shown = _fake_kicad(monkeypatch, p)
    plugin.defaults()
    plugin.Run()
    assert len(shown) == 1 and shown[0].startswith("Placed 0 footprints")


def test_gui_explains_yaml_on_stock_kicad_instead_of_crashing(
    tmp_path: Path, monkeypatch, no_pyyaml
) -> None:
    p = tmp_path / "hdm.yaml"
    p.write_text("components: {}\n")
    plugin, shown = _fake_kicad(monkeypatch, p)
    plugin.defaults()
    plugin.Run()  # must not raise
    assert len(shown) == 1 and "JSON" in shown[0]
