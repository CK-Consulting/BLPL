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


def test_class_assignment_dev04_controlled_impedance() -> None:
    """RGMII / RF_50Ohm / USB2 rules for the dev.04 unified baseboard.

    These are subsystem-prefixed names; the rules must classify them without
    disturbing the bare/anchored USB_D* -> USB3 behaviour above.
    """
    a = _artifact(
        [
            _conn(
                "J_SOM",
                [
                    ("28", "SOM_RGMII_TXD0"),
                    ("39", "SOM_RGMII_RXC"),
                    ("26", "SOM_USB_DP"),
                    ("27", "SOM_USB_DN"),
                ],
            ),
            _conn(
                "J_ETH_SIG",
                [
                    ("6", "ETH_RGMII_TXC"),
                ],
            ),
            _conn(
                "U_GNSS",
                [
                    ("11", "GNSS_RF_IN"),
                    ("14", "GNSS_VDD_RF"),  # power output, must NOT become RF_50Ohm
                ],
            ),
        ]
    )
    nets = {n["name"]: n["class"] for n in s4.synthesize(a)["nets"]}
    assert nets["SOM_RGMII_TXD0"] == "RGMII_Diff"
    assert nets["SOM_RGMII_RXC"] == "RGMII_Diff"
    assert nets["ETH_RGMII_TXC"] == "RGMII_Diff"
    assert nets["SOM_USB_DP"] == "USB2_Diff_90Ohm"
    assert nets["SOM_USB_DN"] == "USB2_Diff_90Ohm"
    assert nets["GNSS_RF_IN"] == "RF_50Ohm"
    # Boundary: a "*_VDD_RF" power output is not an RF feed line.
    assert nets["GNSS_VDD_RF"] == "Default"


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


def test_diff_pair_detection_dp_dn_spelling() -> None:
    """_DP pairs with _DN, not just _DM (dev.04 SOM_USB_DP/DN naming).

    The USB2 classifier accepts _DP/_DN pairs, so the diff-pair detector must tag
    both halves — the stage contract and the CLI's diff-pair count depend on it.
    """
    a = _artifact(
        [
            _conn(
                "J_SOM",
                [
                    ("26", "SOM_USB_DP"),
                    ("27", "SOM_USB_DN"),
                ],
            )
        ]
    )
    nets = {n["name"]: n for n in s4.synthesize(a)["nets"]}
    assert nets["SOM_USB_DP"]["class"] == "USB2_Diff_90Ohm"
    assert nets["SOM_USB_DN"]["class"] == "USB2_Diff_90Ohm"
    assert nets["SOM_USB_DP"]["diff_pair_of"] == "SOM_USB_DN"
    assert nets["SOM_USB_DN"]["diff_pair_of"] == "SOM_USB_DP"


def test_class_assignment_prefixed_power_rails() -> None:
    """Subsystem-prefixed power rails classify as Power_Bulk; enable/RF-supply lines do not."""
    a = _artifact(
        [
            _conn(
                "J1",
                [
                    ("1", "SOM_VIN"),         # power input rail
                    ("2", "ETH_PWR_OUT"),     # regulated DC output
                    ("3", "GNSS_VBCKP"),      # backup power rail
                    ("4", "CELL_USB_VBUS"),   # USB 5V rail (prefixed)
                    ("5", "CELL_PWR_EN"),     # enable/control line -> NOT a rail
                    ("6", "GNSS_VDD_RF"),     # internal RF supply -> stays Default
                    ("7", "GNSS_ANT_ON"),     # antenna power-enable -> stays Default
                ],
            )
        ]
    )
    nets = {n["name"]: n["class"] for n in s4.synthesize(a)["nets"]}
    assert nets["SOM_VIN"] == "Power_Bulk"
    assert nets["ETH_PWR_OUT"] == "Power_Bulk"
    assert nets["GNSS_VBCKP"] == "Power_Bulk"
    assert nets["CELL_USB_VBUS"] == "Power_Bulk"
    # Boundaries: control/enable and internal-supply nets must not become Power_Bulk.
    assert nets["CELL_PWR_EN"] == "Default"
    assert nets["GNSS_VDD_RF"] == "Default"
    assert nets["GNSS_ANT_ON"] == "Default"


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
