"""Unit tests for pipeline.stage4_synthesize_nets."""

from __future__ import annotations

from blpl.core import schema, stage4_synthesize_nets as s4


def _artifact(connectors: list[dict]) -> dict:
    a = {
        "project_id": "proj",
        "schema_version": 1,
        "source_files": [],
        "components": [],
        "connectors": connectors,
        "subsystems": [],
        "raw_nets": [],
    }
    schema.validate("design_artifact", a)
    return a


def _conn(local_id: str, pins: list[tuple[str, str]]) -> dict:
    return {
        "local_id": local_id,
        "pins": [{"pin": p, "signal": s} for p, s in pins],
    }


def test_signals_become_nets_with_members() -> None:
    a = _artifact(
        [
            _conn("J1", [("1", "VIN"), ("2", "GND")]),
            _conn("J2", [("A", "UART_TX"), ("B", "UART_RX"), ("C", "GND")]),
        ]
    )
    result = s4.synthesize(a)
    nets = {n["name"]: n for n in result["nets"]}
    assert set(nets) == {"VIN", "GND", "UART_TX", "UART_RX"}
    gnd_members = sorted(
        [(m["refdes"], m["pin"]) for m in nets["GND"]["members"]]
    )
    assert gnd_members == [("J1", "2"), ("J2", "C")]


def test_nc_signals_are_dropped() -> None:
    a = _artifact([_conn("J1", [("1", "NC"), ("2", "N/C"), ("3", "-"), ("4", "REAL")])])
    result = s4.synthesize(a)
    names = {n["name"] for n in result["nets"]}
    assert names == {"REAL"}


def test_class_assignment_rules() -> None:
    a = _artifact(
        [
            _conn(
                "J1",
                [
                    ("1", "GND"),
                    ("2", "VCC_3V3"),
                    ("3", "USB_DP"),
                    ("4", "USB_DM"),
                    ("5", "USB3_TX_P"),
                    ("6", "PCIE_CLK_P"),
                    ("7", "DDR4_DQ0"),
                    ("8", "RANDOM_SIGNAL"),
                ],
            )
        ]
    )
    nets = {n["name"]: n["class"] for n in s4.synthesize(a)["nets"]}
    assert nets["GND"] == "Power_Bulk"
    assert nets["VCC_3V3"] == "Power_Bulk"
    assert nets["USB_DP"] == "USB3_Diff_90Ohm"
    assert nets["USB3_TX_P"] == "USB3_Diff_90Ohm"
    assert nets["PCIE_CLK_P"] == "PCIe_Diff_85Ohm"
    assert nets["DDR4_DQ0"] == "DDR4_Diff_90Ohm"
    assert nets["RANDOM_SIGNAL"] == "Default"


def test_class_assignment_for_common_variants() -> None:
    a = _artifact(
        [
            _conn(
                "J1",
                [
                    ("1", "3.3V"),       # dotted decimal voltage
                    ("2", "1.8V"),
                    ("3", "USB_D+"),     # +/- suffix form
                    ("4", "USB_D-"),
                    ("5", "USB2_D+"),
                    ("6", "SS_TX+"),     # USB SuperSpeed
                    ("7", "SS_RX-"),
                ],
            )
        ]
    )
    nets = {n["name"]: n["class"] for n in s4.synthesize(a)["nets"]}
    assert nets["3.3V"] == "Power_Bulk"
    assert nets["1.8V"] == "Power_Bulk"
    assert nets["USB_D+"] == "USB3_Diff_90Ohm"
    assert nets["USB_D-"] == "USB3_Diff_90Ohm"
    assert nets["USB2_D+"] == "USB3_Diff_90Ohm"
    assert nets["SS_TX+"] == "USB3_Diff_90Ohm"
    assert nets["SS_RX-"] == "USB3_Diff_90Ohm"


def test_diff_pair_detection_tags_both_halves() -> None:
    a = _artifact(
        [
            _conn(
                "J1",
                [
                    ("1", "USB_D+"),
                    ("2", "USB_D-"),
                    ("3", "USB3_TX_P"),
                    ("4", "USB3_TX_N"),
                    ("5", "SIGNAL_DP"),
                    ("6", "SIGNAL_DM"),
                    ("7", "STANDALONE"),  # no complement → no diff_pair_of
                ],
            )
        ]
    )
    nets = {n["name"]: n for n in s4.synthesize(a)["nets"]}
    assert nets["USB_D+"]["diff_pair_of"] == "USB_D-"
    assert nets["USB_D-"]["diff_pair_of"] == "USB_D+"
    assert nets["USB3_TX_P"]["diff_pair_of"] == "USB3_TX_N"
    assert nets["SIGNAL_DP"]["diff_pair_of"] == "SIGNAL_DM"
    assert "diff_pair_of" not in nets["STANDALONE"]


def test_duplicate_pins_in_a_net_are_deduped() -> None:
    a = _artifact([_conn("J1", [("1", "GND"), ("1", "GND")])])
    result = s4.synthesize(a)
    (gnd,) = result["nets"]
    assert len(gnd["members"]) == 1


def test_bom_refdes_override_is_honoured() -> None:
    a = _artifact([_conn("J_USB_C", [("1", "GND")])])
    bom = {
        "project_id": "proj",
        "schema_version": 1,
        "rows": [
            {
                "local_id": "J_USB_C",
                "refdes": "J5",
                "mpn": "X",
                "package": "Y",
                "confidence": 1.0,
            }
        ],
    }
    schema.validate("bom", bom)
    result = s4.synthesize(a, bom)
    (gnd,) = result["nets"]
    assert gnd["members"][0]["refdes"] == "J5"
