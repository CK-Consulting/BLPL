"""When two passives are one order line, and when they only look like it."""

from __future__ import annotations

import pytest

from blpl.core.passives import (
    equivalence_key,
    extract,
    may_merge,
    required_attributes,
    why_unmergeable,
)


def _c(desc, package="0402", **kw):
    return extract({"description": desc, "package": package, **kw})


# -- reading values ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("10k ±1%", 10_000.0),
        ("4k7 ±1%", 4_700.0),          # multiplier standing in for the decimal
        ("100nF X7R", 100e-9),
        ("2.2uH shielded", 2.2e-6),
        ("470 ohm", 470.0),
    ],
)
def test_values_are_read_in_the_forms_people_write(text, expected):
    assert _c(text).value == pytest.approx(expected)


@pytest.mark.parametrize("text,watts", [("1/16W", 0.0625), ("1/4W", 0.25), ("250mW", 0.25)])
def test_power_is_read_in_fractions_and_decimals(text, watts):
    assert _c(f"10k ±1% {text}").power_w == pytest.approx(watts)


def test_declared_fields_beat_parsed_prose():
    spec = _c("100nF X7R 50V", tolerance=1, voltage_v=100)
    assert spec.tolerance == 1 and spec.voltage_v == 100
    assert "tolerance" not in spec.inferred and "voltage_v" not in spec.inferred


def test_what_was_guessed_is_recorded_as_guessed():
    """A grouping built on parsed prose has to announce itself as provisional
    wherever it surfaces."""
    spec = _c("100nF X7R 50V ±10%")
    assert spec.is_inferred
    assert {"value", "dielectric", "voltage_v", "tolerance"} <= spec.inferred


# -- the rule the module exists for ------------------------------------------


def test_a_safety_cap_never_merges_with_an_ordinary_one():
    """Same nominal value, same package. One may sit across mains and the other
    may not, and after a merge nothing is left to say they differed."""
    ordinary = _c("100nF X7R 50V ±10% decoupling")
    safety = _c("100nF Y2 305VAC X7R ±10% mains")
    ok, _ = may_merge(ordinary, safety, "C")
    assert not ok
    assert equivalence_key(ordinary, "C") != equivalence_key(safety, "C")


def test_a_rated_part_never_merges_with_an_unrated_one():
    """Even with everything else identical: a blank safety class may be an
    unrecorded Y2, and 'may' is not good enough for a purchase order."""
    plain = _c("100nF X7R 50V ±10%")
    rated = _c("100nF X2 50V X7R ±10%")
    ok, why = may_merge(plain, rated, "C")
    assert not ok and "safety class" in why


def test_two_x_and_y_classes_are_not_interchangeable():
    x2 = _c("100nF X2 275VAC X7R ±10%", package="1206")
    y2 = _c("100nF Y2 275VAC X7R ±10%", package="1206")
    assert not may_merge(x2, y2, "C")[0]


def test_a_higher_wattage_resistor_is_a_different_part():
    """The 1/4W was chosen because the 1/16W burns."""
    small = _c("10k ±1% 1/16W")
    big = _c("10k ±1% 1/4W")
    ok, why = may_merge(small, big, "R")
    assert not ok and "power" in why


def test_a_deliberate_requirement_blocks_a_merge():
    plain = _c("10k ±1% 1/4W")
    flame = _c("10k ±1% 1/4W flameproof")
    ok, why = may_merge(plain, flame, "R")
    assert not ok and "flameproof" in why


def test_absence_of_a_required_attribute_is_never_equivalence():
    known = _c("100nF X7R 50V ±10%")
    bare = _c("100nF decoupling")
    ok, why = may_merge(known, bare, "C")
    assert not ok and "not recorded on both" in why
    assert equivalence_key(bare, "C") is None


# -- the useful case ---------------------------------------------------------


def test_the_same_part_from_two_vendors_consolidates():
    """The whole point: one line saying how many to buy."""
    a = _c("100nF X7R 50V ±10%")
    b = _c("100nF X7R 50V ±10%")
    assert may_merge(a, b, "C")[0]
    assert equivalence_key(a, "C") == equivalence_key(b, "C")


def test_requirements_depend_on_what_the_part_is():
    """Requiring a power rating on a capacitor makes every line ungroupable,
    which is its own way of being unsafe: a view nobody can use gets replaced
    by a spreadsheet nobody checks."""
    assert "power_w" in required_attributes("R")
    assert "power_w" not in required_attributes("C")
    assert "dielectric" in required_attributes("C")
    assert "dielectric" not in required_attributes("R")


def test_an_unknown_class_invents_no_requirements():
    assert required_attributes("ZZ") == ()


# -- no substitutions --------------------------------------------------------


def test_no_substitutions_bars_grouping_however_well_specified():
    """'You may order either of these' is precisely the statement the flag
    exists to withhold."""
    plain = _c("100nF X7R 50V ±10%")
    locked = _c("100nF X7R 50V ±10%", no_substitutions=True,
                no_substitutions_reason="VDE approval on this exact part")
    assert may_merge(plain, plain, "C")[0]          # identical parts do merge
    assert not may_merge(plain, locked, "C")[0]     # …until one is locked
    assert equivalence_key(locked, "C") is None


def test_the_refusal_carries_the_reason():
    """A flag without a reason gets overridden by whoever is under deadline."""
    locked = _c("100nF X7R 50V", no_substitutions=True,
                no_substitutions_reason="sole-source qualified")
    _, why = may_merge(locked, _c("100nF X7R 50V"), "C")
    assert "sole-source qualified" in why
    assert "sole-source qualified" in why_unmergeable(locked, "C")


def test_a_locked_line_without_a_reason_still_refuses():
    locked = _c("100nF X7R 50V", no_substitutions=True)
    ok, why = may_merge(locked, _c("100nF X7R 50V"), "C")
    assert not ok and "no reason recorded" in why
