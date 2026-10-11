"""Unit tests for pipeline.component_classifier."""

from __future__ import annotations

from pathlib import Path

from blpl.classifier.component_classifier import classify


_SYMBOLS_ROOT = Path(__file__).resolve().parent.parent / "kicad-symbols"
_FOOTPRINTS_ROOT = Path(__file__).resolve().parent.parent / "kicad-footprints"


def _call(row: dict):
    return classify(row, symbols_root=_SYMBOLS_ROOT, footprints_root=_FOOTPRINTS_ROOT)


def test_resistor_0603_classifies_as_passive_identity() -> None:
    result = _call(
        {
            "mpn": "RC0603FR-0710KL",
            "description": "Resistor 10k 1% 0603",
            "package": "0603",
        }
    )
    assert result.bucket == "passive_2pin"
    assert result.lib_symbol == "Device:R"
    assert result.lib_footprint == "Resistor_SMD:R_0603_1608Metric"
    assert result.pin_map == {"1": "1", "2": "2"}
    assert result.source == "passive_identity"
    assert result.is_auto_resolved


def test_capacitor_0402_picks_correct_package() -> None:
    result = _call({"mpn": "GRM155", "description": "Capacitor 10nF MLCC 0402"})
    assert result.bucket == "passive_2pin"
    assert result.lib_symbol == "Device:C"
    assert result.lib_footprint.startswith("Capacitor_SMD:C_0402")


def test_passive_with_unknown_size_falls_back_to_0603() -> None:
    result = _call({"mpn": "LED0000", "description": "LED red"})
    assert result.bucket == "passive_2pin"
    assert result.lib_symbol == "Device:LED"
    assert "0603" in result.lib_footprint


def test_barrel_jack_maps_to_connector_barrel_jack() -> None:
    result = _call({"mpn": "PJ-063AH", "description": "Barrel jack 5.5mm power"})
    assert result.bucket == "generic_connector"
    assert result.lib_symbol == "Connector:Barrel_Jack"
    # Barrel_Jack has unnamed pins → identity map.
    assert result.pin_map == {"1": "1", "2": "2"}
    assert result.source == "connector_lookup"


def test_usb_c_receptacle_14p_derives_named_pin_map() -> None:
    result = _call(
        {
            "mpn": "USB4085-GF-A",
            "description": "USB-C Receptacle USB2.0 14-pin",
        }
    )
    assert result.bucket == "generic_connector"
    assert result.lib_symbol == "Connector:USB_C_Receptacle_USB2.0_14P"
    # Known pins for USB-C (any pin number KiCad assigns is fine — verify the
    # logical→physical mapping exists and non-empty).
    assert "GND" in result.pin_map
    assert "VBUS" in result.pin_map
    assert "CC1" in result.pin_map
    # Duplicate-signal warning should surface (GND appears on A1, A12, B1, B12).
    assert any("GND" in w for w in result.warnings)


def test_mini_pcie_maps_to_bus_pci_express_mini() -> None:
    result = _call({"mpn": "MM60-52B1", "description": "Mini-PCIe Full-size slot"})
    assert result.bucket == "generic_connector"
    assert result.lib_symbol == "Connector:Bus_PCI_Express_Mini"
    assert len(result.pin_map) > 20  # 52-pin connector, but some pins share signals


def test_ffc_parametric_by_pin_count() -> None:
    result = _call(
        {"mpn": "FH12-40S", "description": "FFC/FPC 40-pin 0.5mm horizontal", "pin_count": 40}
    )
    assert result.bucket == "generic_connector"
    assert result.lib_symbol == "Connector_Generic:Conn_01x40"


def test_nmos_sot23_classifies_as_small_signal_3pin() -> None:
    result = _call(
        {
            "mpn": "2N7002",
            "description": "N-channel MOSFET 60V",
            "package": "SOT-23",
        }
    )
    assert result.bucket == "small_signal_3pin"
    assert result.lib_symbol == "Device:Q_NMOS_GDS"
    assert result.pin_map == {"G": "1", "S": "2", "D": "3"}


def test_fpga_bga_classifies_as_specific() -> None:
    result = _call(
        {
            "mpn": "XAZU1EG-1FBG484E",
            "description": "Xilinx Zynq UltraScale+ ZU1EG FPGA BGA-484",
            "package": "BGA-484_19x19mm_P0.8mm",
        }
    )
    assert result.bucket == "specific"
    assert not result.is_auto_resolved


def test_unclassified_row_returns_specific() -> None:
    result = _call({"mpn": "CUSTOM_ASIC_X", "description": "Custom SoC package"})
    assert result.bucket == "specific"


def test_mosfet_without_package_hint_does_not_match() -> None:
    """A MOSFET claim with no SOT-23 package hint is left specific — we don't
    want to assume the pin map of a random 3-pin package."""
    result = _call(
        {"mpn": "NVMFS5C628NL", "description": "N-channel MOSFET 30V", "package": "PQFN"}
    )
    assert result.bucket == "specific"
