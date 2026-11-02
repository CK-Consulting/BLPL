"""The emitted board carries its physical stackup."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from blpl.emitter import pcb, sexpr

OUTLINE = [(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)]


def _hdm(stackup: dict) -> dict:
    return {
        "project": {"name": "t", "dimensions": [40, 30], "stackup": stackup},
        "components": {}, "nets": {},
        "board_geometry": {"outline": {"segments": [{"kind": "line", "points": [OUTLINE[i], OUTLINE[(i + 1) % 4]]}
                                                    for i in range(4)], "polygon": OUTLINE},
                           "features": [], "planes": []},
    }


def _layers(node):
    st = sexpr.find(sexpr.find(node, "setup"), "stackup")
    return [(sexpr.unquote(l[1]), sexpr.unquote(sexpr.find(l, "type")[1]),
             float(sexpr.find(l, "thickness")[1]) if sexpr.find(l, "thickness") else 0.0)
            for l in sexpr.find_all(st, "layer")], st


def test_a_four_layer_board_has_its_copper_and_dielectric_in_order(tmp_path):
    layers, st = _layers(pcb.build(_hdm({"layers": 4, "thickness": 1.0, "finish": "ENIG"}), footprints_root=tmp_path))
    copper = [n for n, t, _ in layers if t == "copper"]
    assert copper == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    kinds = [t for _, t, _ in layers if t in ("prepreg", "core")]
    assert kinds == ["prepreg", "core", "prepreg"]
    # The board's thickness is its layers' sum.
    assert sum(th for _, _, th in layers) == pytest.approx(1.0, abs=1e-3)
    assert sexpr.unquote(sexpr.find(st, "copper_finish")[1]) == "ENIG"


def test_the_designer_can_give_the_dielectric(tmp_path):
    # 1.6 - 0.02 mask - 0.105 copper = 1.475 of dielectric: 2 x 0.1 + 1.275.
    layers, _ = _layers(pcb.build(_hdm({"layers": 4, "thickness": 1.6, "prepreg": 0.1, "core": 1.275, "er": 4.2}),
                                  footprints_root=tmp_path))
    pp = [th for _, t, th in layers if t == "prepreg"]
    assert pp == [0.1, 0.1] and [th for _, t, th in layers if t == "core"] == [1.275]


def test_a_given_dielectric_that_does_not_add_up_is_refused(tmp_path):
    with pytest.raises(ValueError, match="must add up"):
        pcb.build(_hdm({"layers": 4, "thickness": 1.6, "prepreg": 0.1, "core": 1.2}), footprints_root=tmp_path)


def test_a_given_prepreg_is_kept_or_refused_never_rescaled(tmp_path):
    with pytest.raises(ValueError, match="prepreg 0.2 mm leaves"):
        pcb.build(_hdm({"layers": 4, "thickness": 0.5, "prepreg": 0.2}), footprints_root=tmp_path)
    layers, _ = _layers(pcb.build(_hdm({"layers": 4, "thickness": 1.0, "prepreg": 0.2}), footprints_root=tmp_path))
    assert [th for _, t, th in layers if t == "prepreg"] == [0.2, 0.2]


def test_a_two_layer_board_is_one_core(tmp_path):
    layers, _ = _layers(pcb.build(_hdm({"layers": 2, "thickness": 1.6}), footprints_root=tmp_path))
    assert [t for _, t, _ in layers if t in ("prepreg", "core")] == ["core"]


def test_a_two_layer_board_refuses_a_prepreg_it_has_nowhere_to_put(tmp_path):
    with pytest.raises(ValueError, match="no prepreg"):
        pcb.build(_hdm({"layers": 2, "thickness": 1.6, "prepreg": 0.2, "core": 1.51}), footprints_root=tmp_path)
    layers, _ = _layers(pcb.build(_hdm({"layers": 2, "thickness": 1.6, "core": 1.51}), footprints_root=tmp_path))
    assert [(t, th) for _, t, th in layers if t in ("prepreg", "core")] == [("core", 1.51)]


def test_a_one_layer_board_claims_no_bottom_copper(tmp_path):
    setup = sexpr.find(pcb.build(_hdm({"layers": 1, "thickness": 1.6}), footprints_root=tmp_path), "setup")
    assert sexpr.find(setup, "stackup") is None


def test_the_review_finds_each_outer_layers_reference_plane(tmp_path):
    """The point of it: with no stackup the design review measured F.Cu
    against B.Cu, and reported every signal on verdacor as off its plane."""
    kh = Path(__file__).resolve().parents[1] / "kicad-happy" / "skills" / "kicad" / "scripts"
    if not (kh / "analyze_pcb.py").is_file():
        pytest.skip("needs the kicad-happy submodule")
    board = tmp_path / "b.kicad_pcb"
    board.write_text(pcb.emit(_hdm({"layers": 4, "thickness": 1.0}), footprints_root=tmp_path))
    # Ask the analyzer's own stackup reader and reference map.
    sys.path.insert(0, str(kh))
    try:
        import analyze_pcb as a  # noqa: PLC0415
    finally:
        sys.path.remove(str(kh))
    setup = a.extract_setup(a.parse_file(str(board))) if hasattr(a, "extract_setup") else None
    if setup is None:
        pytest.skip("this kicad-happy has no setup extractor to ask")
    ref = a._build_reference_layer_map(setup.get("stackup", []))
    assert ref["F.Cu"] == "In1.Cu" and ref["B.Cu"] == "In2.Cu"


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli")
def test_kicad_reads_the_stackup(tmp_path):
    board = tmp_path / "b.kicad_pcb"
    board.write_text(pcb.emit(_hdm({"layers": 4, "thickness": 1.0}), footprints_root=tmp_path))
    proc = subprocess.run(["kicad-cli", "pcb", "upgrade", "--force", str(board)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "(stackup" in board.read_text() and '"dielectric 2"' in board.read_text()


def test_the_schema_takes_the_dielectric_and_refuses_nonsense():
    from blpl.core import schema

    def cfg(**st):
        return {"project": {"name": "p", "dimensions": [10, 10], "stackup": {"layers": 4, **st}}}

    schema.validate("project_config", cfg(prepreg=0.1, core=1.2, er=4.2))
    for bad in ({"er": 1}, {"prepreg": 0}, {"core": 9}):
        with pytest.raises(Exception):
            schema.validate("project_config", cfg(**bad))


@pytest.mark.parametrize("copper", [6, 8, 10, 12])
def test_the_stackup_always_adds_up_to_the_declared_thickness(tmp_path, copper):
    layers, _ = _layers(pcb.build(_hdm({"layers": copper, "thickness": 1.6}), footprints_root=tmp_path))
    assert sum(th for _, _, th in layers) == pytest.approx(1.6, abs=1e-3)
    assert min(th for _, t, th in layers if t in ("prepreg", "core")) >= 0.05


def test_a_stackup_that_cannot_fit_is_refused(tmp_path):
    with pytest.raises(ValueError, match="per dielectric"):
        pcb.build(_hdm({"layers": 12, "thickness": 0.6}), footprints_root=tmp_path)



# -- fabrication ---------------------------------------------------------------


def _fab(fabrication: dict, stackup: dict | None = None):
    h = _hdm(stackup or {"layers": 4, "thickness": 1.6, "finish": "ENIG"})
    h["fabrication"] = fabrication
    return h


def test_no_fabrication_section_emits_the_board_exactly_as_before(tmp_path):
    """Every existing project has no fabrication: its board must not change."""
    plain = pcb.emit(_hdm({"layers": 4, "thickness": 1.6, "finish": "ENIG"}), footprints_root=tmp_path)
    assert pcb.emit(_fab({}), footprints_root=tmp_path) == plain
    assert "(color" not in plain and "edge_connector" not in plain and '"FR4"' in plain


def test_copper_weight_laminate_and_colours_reach_the_stackup(tmp_path):
    layers, st = _layers(pcb.build(_fab({"copper_oz": {"outer": 2, "inner": 1},
                                         "material": "FR-4 High Tg", "mask_color": "black",
                                         "silk_color": "white"}), footprints_root=tmp_path))
    cu = {n: th for n, t, th in layers if t == "copper"}
    assert cu["F.Cu"] == pytest.approx(0.07) and cu["In1.Cu"] == pytest.approx(0.035)
    # Heavier copper still adds up to the declared thickness: the dielectric gives way.
    assert sum(th for _, _, th in layers) == pytest.approx(1.6, abs=1e-3)
    text = sexpr.dump(st)
    assert '"FR-4 High Tg"' in text and '"FR4"' not in text
    assert text.count('(color "Black")') == 2 and text.count('(color "White")') == 2


def test_gold_fingers_and_castellation_become_kicads_edge_flags(tmp_path):
    _layers_, st = _layers(pcb.build(_fab({"gold_fingers": {"bevel_deg": 30}, "castellated": True}),
                                     footprints_root=tmp_path))
    assert sexpr.find(st, "edge_connector")[1] == "bevelled"
    assert sexpr.find(st, "edge_plating")[1] == "yes"
    _l, flat = _layers(pcb.build(_fab({"gold_fingers": {"bevel_deg": 0}}), footprints_root=tmp_path))
    assert sexpr.find(flat, "edge_connector")[1] == "yes"


@pytest.mark.parametrize("protection, want", [
    ("none", {"tenting": ["no", "no"]}),
    ("plugged", {"tenting": ["yes", "yes"], "plugging": ["yes", "yes"]}),
    ("filled_capped", {"filling": "yes", "capping": "yes"}),
])
def test_via_protection_is_kicads_ipc_4761_setting(tmp_path, protection, want):
    setup = sexpr.find(pcb.build(_fab({"via_protection": protection}), footprints_root=tmp_path), "setup")
    for key, value in want.items():
        node = sexpr.find(setup, key)
        if isinstance(value, list):
            assert [sexpr.find(node, side)[1] for side in ("front", "back")] == value
        else:
            assert node[1] == value


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli")
def test_kicad_keeps_every_fabrication_setting(tmp_path):
    """Written in a form KiCad reads: a board it loads and saves again still
    says the same thing (a castellated_pads flag did not survive this)."""
    board = tmp_path / "b.kicad_pcb"
    board.write_text(pcb.emit(_fab({"copper_oz": {"outer": 2}, "material": "FR-4 High Tg",
                                    "mask_color": "black", "silk_color": "white",
                                    "gold_fingers": {"bevel_deg": 30}, "castellated": True,
                                    "via_protection": "filled_capped"}), footprints_root=tmp_path))
    proc = subprocess.run(["kicad-cli", "pcb", "upgrade", "--force", str(board)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    text = " ".join(board.read_text().split())
    for want in ('(color "Black")', '(color "White")', '(material "FR-4 High Tg")', "(thickness 0.07)",
                 "(edge_connector bevelled)", "(edge_plating yes)", "(capping yes)", "(filling yes)"):
        assert want in text, want


def test_the_schema_takes_fabrication_and_refuses_what_a_fab_cannot_read():
    from blpl.core import schema

    def cfg(**fab):
        return {"project": {"name": "p", "dimensions": [10, 10]}, "fabrication": fab}

    schema.validate("project_config", cfg(copper_oz={"outer": 1, "inner": 0.5}, tg_c=170,
                                          mask_color="black", gold_fingers=False,
                                          via_protection="plugged", castellated=True))
    schema.validate("project_config", cfg(gold_fingers={"bevel_deg": 20}))
    for bad in ({"mask_color": "pink"}, {"via_protection": "glued"}, {"copper_oz": {"outer": 9}},
                {"gold_fingers": True}, {"tg_c": 20}, {"colour": "green"}):
        with pytest.raises(Exception):
            schema.validate("project_config", cfg(**bad))


def _table(node) -> list[str]:
    return [sexpr.unquote(row[1]) for row in sexpr.find(node, "layers")[1:]]


def test_a_none_colour_takes_its_layers_off_the_board(tmp_path):
    """No mask ordered, no mask on the board: the layer table and the stackup
    both lose it, so every export agrees with board-specs.md (Codex, PR #98)."""
    node = pcb.build(_fab({"mask_color": "none", "silk_color": "white"}), footprints_root=tmp_path)
    table = _table(node)
    assert "F.Mask" not in table and "B.Mask" not in table
    assert {"F.SilkS", "B.SilkS", "F.Paste", "Edge.Cuts", "F.Cu", "B.Cu"} <= set(table)
    names = [n for n, _, _ in _layers(node)[0]]
    assert "F.Mask" not in names and "B.Mask" not in names and "F.SilkS" in names

    node = pcb.build(_fab({"silk_color": "none"}), footprints_root=tmp_path)
    assert not {"F.SilkS", "B.SilkS"} & set(_table(node))
    assert {"F.Mask", "B.Mask"} <= set(_table(node))


def test_a_coloured_or_unset_mask_keeps_every_layer(tmp_path):
    plain = _table(pcb.build(_hdm({"layers": 2, "thickness": 1.6}), footprints_root=tmp_path))
    assert _table(pcb.build(_fab({"mask_color": "green", "silk_color": "black"},
                                 {"layers": 2, "thickness": 1.6}), footprints_root=tmp_path)) == plain


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli")
def test_kicad_keeps_the_layers_off_and_odb_leaves_them_out(tmp_path):
    """ODB++ takes no layer selection, so the board itself must not have the
    layer; and KiCad must not put it back when it saves the board."""
    import zipfile

    board = tmp_path / "b.kicad_pcb"
    board.write_text(pcb.emit(_fab({"mask_color": "none", "silk_color": "none"}), footprints_root=tmp_path))
    proc = subprocess.run(["kicad-cli", "pcb", "upgrade", "--force", str(board)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    saved = board.read_text()
    assert '"F.Mask" user' not in saved and '"F.SilkS" user' not in saved
    odb = tmp_path / "b-odb.zip"
    proc = subprocess.run(["kicad-cli", "pcb", "export", "odb", "--output", str(odb), str(board)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    layer_dirs = {n.lower() for n in zipfile.ZipFile(odb).namelist() if "/layers/" in n.lower()}
    assert layer_dirs, "the ODB++ job has no layers at all"
    assert not [n for n in layer_dirs if "mask" in n or "silk" in n]


@pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="needs kicad-cli")
def test_footprints_still_naming_a_removed_layer_load_and_export(tmp_path):
    """Pads keep naming F.Mask and footprints keep their silk; a board with
    those layers disabled must still load, save and export."""
    import zipfile

    fixture = Path(__file__).parent / "fixtures" / "footprint_flip" / "kicad_placed.kicad_pcb"
    node = sexpr.parse(fixture.read_text(encoding="utf-8"))
    table = sexpr.find(node, "layers")
    gone = {"F.Mask", "B.Mask", "F.SilkS", "B.SilkS"}
    kept = [row for row in table[1:] if sexpr.unquote(row[1]) not in gone]
    assert len(table) - 1 - len(kept) == len(gone)
    table[1:] = kept
    text = sexpr.dump_top(node)
    assert '"F.Mask"' in text, "the pads should still name the removed layer"
    board = tmp_path / "b.kicad_pcb"
    board.write_text(text, encoding="utf-8")
    odb = tmp_path / "b-odb.zip"
    proc = subprocess.run(["kicad-cli", "pcb", "export", "odb", "--output", str(odb), str(board)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    names = [n.lower() for n in zipfile.ZipFile(odb).namelist()]
    assert any("/layers/f.cu/" in n for n in names)
    assert not [n for n in names if "/layers/" in n and ("mask" in n or "silk" in n)]


@pytest.mark.parametrize("copper", [2, 4])
def test_a_board_with_no_mask_still_adds_up_to_its_thickness(tmp_path, copper):
    """The mask's 0.01 mm a side went unused when the mask did: a 1.6 mm board
    emitted 1.58 mm of layers (Codex, PR #99)."""
    layers, _ = _layers(pcb.build(_fab({"mask_color": "none"}, {"layers": copper, "thickness": 1.6}),
                                  footprints_root=tmp_path))
    assert "F.Mask" not in [n for n, _, _ in layers]
    assert sum(th for _, _, th in layers) == pytest.approx(1.6, abs=1e-3)


def test_a_given_core_is_checked_against_the_board_with_no_mask(tmp_path):
    """1.6 mm less two 1 oz layers is 1.53 mm of core when there is no mask."""
    _layers(pcb.build(_fab({"mask_color": "none"}, {"layers": 2, "thickness": 1.6, "core": 1.53}),
                      footprints_root=tmp_path))
