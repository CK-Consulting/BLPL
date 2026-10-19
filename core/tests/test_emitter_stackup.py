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
