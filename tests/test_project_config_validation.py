"""A board of the wrong size is not a warning.

Stage 5 read the board outline as
``project_config.get("project", {}).get("dimensions") or [100, 80]``. A
misspelled key, a string where a number belongs, a one-element list — none of
them errored. They silently produced a 100 x 80 mm board that built, routed and
reported success at the wrong size. example-handheld's core board was
100 x 80 for weeks against a 96.85 x 57.14 target and nothing said so; it
surfaced only when a placer started measuring how full the board was.

Validation is on *load* rather than on write because a form can only police
what it writes. A hand-edit, a git merge, a template copied from another board
and a file the pipeline has never seen all arrive the same way.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from blpl.core.stage5_emit_yaml_hdm import InvalidProjectConfigError, ensure_project_config

MINIMAL = {
    "project": {"name": "p", "dimensions": [30.0, 35.0]},
    "net_classes": {"Default": {"trace_width": 0.15, "clearance": 0.15}},
}


def write(tmp_path: Path, config) -> Path:
    p = tmp_path / "project.yaml"
    p.write_text(yaml.safe_dump(config), encoding="utf-8")
    return p


def test_a_valid_config_loads(tmp_path):
    assert ensure_project_config(write(tmp_path, MINIMAL), "p")["project"]["dimensions"] == [30.0, 35.0]


def test_a_misspelled_dimensions_key_is_refused_not_defaulted(tmp_path):
    """The exact failure that shipped a wrong-sized board."""
    bad = {"project": {"name": "p", "dimensons": [96.85, 57.14]}}
    with pytest.raises(InvalidProjectConfigError) as e:
        ensure_project_config(write(tmp_path, bad), "p")
    assert "dimensions" in str(e.value)
    assert "100" not in str(e.value), "it must not offer the old silent default as an answer"


def test_a_string_where_a_number_belongs_is_refused(tmp_path):
    bad = {"project": {"name": "p", "dimensions": ["96.85", "57.14"]}}
    with pytest.raises(InvalidProjectConfigError):
        ensure_project_config(write(tmp_path, bad), "p")


def test_a_one_element_dimension_list_is_refused(tmp_path):
    """Not a square board — a typo."""
    bad = {"project": {"name": "p", "dimensions": [96.85]}}
    with pytest.raises(InvalidProjectConfigError):
        ensure_project_config(write(tmp_path, bad), "p")


def test_a_zero_or_negative_dimension_is_refused(tmp_path):
    for dims in ([0, 40], [-5, 40]):
        with pytest.raises(InvalidProjectConfigError):
            ensure_project_config(write(tmp_path, {"project": {"name": "p", "dimensions": dims}}), "p")


def test_a_stray_zero_is_caught_by_the_upper_bound(tmp_path):
    """968.5 x 57.14 is a fat finger, not a panel."""
    bad = {"project": {"name": "p", "dimensions": [9685.0, 57.14]}}
    with pytest.raises(InvalidProjectConfigError):
        ensure_project_config(write(tmp_path, bad), "p")


def test_an_odd_layer_count_is_refused(tmp_path):
    """Three-layer boards are not manufacturable, so the enum is the whole list."""
    bad = {"project": {"name": "p", "dimensions": [30, 35], "stackup": {"layers": 3}}}
    with pytest.raises(InvalidProjectConfigError):
        ensure_project_config(write(tmp_path, bad), "p")


def test_net_classes_without_default_is_refused(tmp_path):
    """Stage 4 assigns every unmatched net to Default, so the emitted .kicad_pro
    would name a class that does not exist."""
    bad = dict(MINIMAL, net_classes={"Power_Bulk": {"trace_width": 0.4}})
    with pytest.raises(InvalidProjectConfigError):
        ensure_project_config(write(tmp_path, bad), "p")


def test_an_unknown_test_point_policy_is_refused(tmp_path):
    bad = dict(MINIMAL, test_points={"policy": "sometimes"})
    with pytest.raises(InvalidProjectConfigError):
        ensure_project_config(write(tmp_path, bad), "p")


def test_a_placement_hint_with_x_but_no_y_is_refused(tmp_path):
    """Half a coordinate places nothing, and would be read as no hint at all."""
    bad = dict(MINIMAL, placement={"hints": {"U1": {"x": 10}}})
    with pytest.raises(InvalidProjectConfigError):
        ensure_project_config(write(tmp_path, bad), "p")


def test_a_valid_placement_hint_is_accepted(tmp_path):
    ok = dict(MINIMAL, placement={"hints": {"L_DCC": {"near": "U_BLE"}, "U1": {"x": 10, "y": 12, "rot": 90}}})
    assert ensure_project_config(write(tmp_path, ok), "p")["placement"]["hints"]["L_DCC"]["near"] == "U_BLE"


def test_a_non_orthogonal_rotation_is_refused(tmp_path):
    """KiCad footprints place at 0/90/180/270; 45 would silently round."""
    bad = dict(MINIMAL, placement={"hints": {"U1": {"rot": 45}}})
    with pytest.raises(InvalidProjectConfigError):
        ensure_project_config(write(tmp_path, bad), "p")


def test_the_error_names_every_bad_field_not_just_the_first(tmp_path):
    """Fixing a config one error per run is how people give up on a config."""
    bad = {"project": {"name": "p", "dimensions": [0, 35], "stackup": {"layers": 3}}}
    with pytest.raises(InvalidProjectConfigError) as e:
        ensure_project_config(write(tmp_path, bad), "p")
    msg = str(e.value)
    assert "dimensions" in msg and "layers" in msg


def test_every_real_board_config_in_the_repo_validates():
    """The schema has to describe the files that already exist, or it is a
    schema for a different program."""
    root = Path("app/data/projects/example-handheld")
    found = sorted(root.glob("*/project.yaml"))
    if not found:
        pytest.skip("example-handheld is not present")
    for path in found:
        ensure_project_config(path, "example-handheld")
