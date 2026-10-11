"""A net named after a rail is not always that rail."""

from __future__ import annotations

import pytest

from blpl.core.stage4_synthesize_nets import _assign_class


@pytest.mark.parametrize(
    "name",
    ["VBUS_SNS", "VSYS_MON", "REG3V3_FB", "VBAT_SENSE", "VIN_DIV", "PVDD_FEEDBACK"],
)
def test_a_sense_node_is_not_a_power_rail(name: str) -> None:
    """Divider midpoints, feedback nodes and sense taps carry microamps.

    Classing them Power_Bulk gave them a 0.4 mm power trace — on a divider
    midpoint that is coupling area and nothing else — and made the schematic
    emitter put a PWR_FLAG on them, which ERC then reports as a power output
    tied to the GPIO reading them. VBUS_SNS was the last ERC violation on
    example-handheld's core board: two resistors, a cap and an ADC pin.
    """
    assert _assign_class(name) == "Default"


@pytest.mark.parametrize(
    "name", ["VBUS", "VBUS_F", "3V3", "GND", "PGND", "VBAT_PACK", "VSYS", "SOM_VIN"]
)
def test_real_rails_are_still_rails(name: str) -> None:
    assert _assign_class(name) == "Power_Bulk"


@pytest.mark.parametrize("name", ["AVDD_REF", "VDDA_ADC"])
def test_supplies_that_merely_sound_analog_keep_their_copper(name: str) -> None:
    """_REF and _ADC are left out of the rule on purpose: these are supply pins."""
    assert _assign_class(name) == "Power_Bulk"
