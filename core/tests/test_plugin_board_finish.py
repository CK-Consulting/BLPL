"""stage6-plugin's board gets the stackup and fabrication stage6 writes.

pcbnew wrote the plugin's board with no stackup at all and KiCad's own via
tenting, so the two Stage 6s produced different physical boards from one HDM:
the plugin's had no thicknesses, no materials, no colours, mask on a board
ordered without it, and no reference planes for the design review to find.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from blpl.emitter import pcb, sexpr

OUTLINE = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]
_PLUGIN = Path(__file__).resolve().parents[2] / "plugin"


def _hdm(stackup: dict | None = None, fabrication: dict | None = None) -> dict:
    h = {"project": {"name": "t", "dimensions": [40, 30],
                     "stackup": stackup or {"layers": 4, "thickness": 1.6, "finish": "ENIG"}},
         "components": {}, "nets": {},
         "board_geometry": {"outline": {"segments": [{"kind": "line", "points": [OUTLINE[i], OUTLINE[(i + 1) % 4]]}
                                                     for i in range(4)], "polygon": OUTLINE},
                            "features": [], "planes": []}}
    if fabrication is not None:
        h["fabrication"] = fabrication
    return h


def _as_pcbnew_saves_it(hdm: dict, tmp_path) -> sexpr.Node:
    """The emitter's board as the plugin left it: no stackup, KiCad's via
    settings, and setup entries the emitter never writes."""
    node = pcb.build(hdm, footprints_root=tmp_path)
    node[node.index(sexpr.find(node, "setup"))] = [
        "setup", ["pad_to_mask_clearance", "0"], ["tenting", ["front", "yes"], ["back", "yes"]],
        ["covering", ["front", "no"], ["back", "no"]], ["plugging", ["front", "no"], ["back", "no"]],
        ["capping", "no"], ["filling", "no"], ["pcbplotparams", ["outputdirectory", '""']]]
    return node


def test_the_plugins_board_gets_the_emitters_stackup(tmp_path):
    hdm = _hdm(fabrication={"copper_oz": {"outer": 2}, "material": "FR-4 High Tg", "mask_color": "black"})
    finished = pcb.finish_plugin_board(_as_pcbnew_saves_it(hdm, tmp_path), hdm)
    want = sexpr.find(sexpr.find(pcb.build(hdm, footprints_root=tmp_path), "setup"), "stackup")
    setup = sexpr.find(finished, "setup")
    assert setup[1] == want, "the stackup leads setup, as KiCad writes it"
    # What KiCad wrote and the emitter does not decide is kept.
    assert sexpr.find(setup, "pcbplotparams") is not None and sexpr.find(setup, "covering") is not None


def test_a_board_with_no_fabrication_still_gets_a_stackup(tmp_path):
    """The bug was not only fabrication: the plugin wrote no stackup at all."""
    hdm = _hdm()
    finished = pcb.finish_plugin_board(_as_pcbnew_saves_it(hdm, tmp_path), hdm)
    names = [sexpr.unquote(l[1]) for l in sexpr.find_all(sexpr.find(sexpr.find(finished, "setup"), "stackup"), "layer")]
    assert names[:3] == ["F.SilkS", "F.Paste", "F.Mask"] and "In2.Cu" in names


@pytest.mark.parametrize("protection, want", [
    ("filled_capped", {"tenting": ["yes", "yes"], "plugging": ["no", "no"], "filling": "yes", "capping": "yes"}),
    ("none", {"tenting": ["no", "no"], "filling": "no", "capping": "no"}),
    ("plugged", {"plugging": ["yes", "yes"]}),
])
def test_via_protection_replaces_kicads_defaults(tmp_path, protection, want):
    hdm = _hdm(fabrication={"via_protection": protection})
    setup = sexpr.find(pcb.finish_plugin_board(_as_pcbnew_saves_it(hdm, tmp_path), hdm), "setup")
    for key, value in want.items():
        assert len(sexpr.find_all(setup, key)) == 1, key
        node = sexpr.find(setup, key)
        if isinstance(value, list):
            assert [sexpr.find(node, side)[1] for side in ("front", "back")] == value
        else:
            assert node[1] == value


def test_a_none_colour_takes_the_layer_off_the_plugins_board(tmp_path):
    hdm = _hdm(fabrication={"mask_color": "none", "silk_color": "none"})
    board = pcb.build(_hdm(), footprints_root=tmp_path)  # every layer, as pcbnew writes them
    finished = pcb.finish_plugin_board(board, hdm)
    table = [sexpr.unquote(r[1]) for r in sexpr.find(finished, "layers")[1:]]
    assert not {"F.Mask", "B.Mask", "F.SilkS", "B.SilkS"} & set(table)
    assert {"F.Cu", "In1.Cu", "Edge.Cuts", "F.Paste"} <= set(table)


def test_the_boards_thickness_is_the_projects(tmp_path):
    hdm = _hdm({"layers": 2, "thickness": 1.0})
    board = _as_pcbnew_saves_it(hdm, tmp_path)
    sexpr.find(board, "general")[1:] = [["thickness", "1.6"], ["legacy_teardrops", "no"]]
    general = sexpr.find(pcb.finish_plugin_board(board, hdm), "general")
    assert sexpr.find_all(general, "thickness") == [["thickness", "1.0"]]


def test_a_board_whose_copper_is_not_the_stackups_is_refused(tmp_path):
    """A stackup for four layers on a two-layer board would describe a board
    nobody built."""
    board = pcb.build(_hdm({"layers": 2, "thickness": 1.6}), footprints_root=tmp_path)
    with pytest.raises(ValueError, match="copper layers"):
        pcb.finish_plugin_board(board, _hdm({"layers": 4, "thickness": 1.6}))


def _kicad_python() -> str | None:
    for py in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if Path(py).is_file() and subprocess.run([py, "-c", "import pcbnew"],
                                                 capture_output=True).returncode == 0:
            return py
    return None


@pytest.mark.skipif(_kicad_python() is None or shutil.which("kicad-cli") is None,
                    reason="needs KiCad's Python (pcbnew) and kicad-cli")
def test_the_real_plugin_board_keeps_everything_through_kicad(tmp_path):
    """Built by pcbnew, finished here, then loaded and saved by KiCad: the
    stackup and fabrication survive, and ODB++ has no mask when none was asked."""
    hdm = _hdm(fabrication={"mask_color": "none", "silk_color": "white", "material": "FR-4 High Tg",
                            "copper_oz": {"outer": 2}, "via_protection": "filled_capped"})
    hdm_json = tmp_path / "hdm.json"
    hdm_json.write_text(json.dumps(hdm))
    board = tmp_path / "b.kicad_pcb"
    proc = subprocess.run([_kicad_python(), "-m", "blpl_kicad_plugin.build_pcb", "--hdm", str(hdm_json),
                           "--out", str(board), "--footprints-root", str(tmp_path)],
                          capture_output=True, text=True, timeout=300,
                          env={"PYTHONPATH": str(_PLUGIN), "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, proc.stderr
    assert "(stackup" not in board.read_text(), "pcbnew started writing a stackup: this finish may be moot"
    board.write_text(sexpr.dump_top(pcb.finish_plugin_board(sexpr.parse(board.read_text()), hdm)))
    proc = subprocess.run(["kicad-cli", "pcb", "upgrade", "--force", str(board)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    text = " ".join(board.read_text().split())
    for want in ("(stackup", '(material "FR-4 High Tg")', "(thickness 0.07)", '(color "White")',
                 "(capping yes)", "(filling yes)"):
        assert want in text, want
    assert '"F.Mask" user' not in text
    odb = tmp_path / "b-odb.zip"
    proc = subprocess.run(["kicad-cli", "pcb", "export", "odb", "--output", str(odb), str(board)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    names = [n.lower() for n in zipfile.ZipFile(odb).namelist()]
    assert any("/layers/f.cu/" in n for n in names)
    assert not [n for n in names if "/layers/" in n and "mask" in n]
