"""Unit tests for pipeline.connector_synthesis."""

from __future__ import annotations

from blpl.classifier.connector_synthesis import (
    infer_connector_metadata,
    synthesize_bom_rows,
)


def _connector(local_id: str, signals: list[str]) -> dict:
    """Build a minimal design_artifact-shaped connector dict."""
    pins = [
        {"pin": str(i + 1), "signal": sig, "function": "", "voltage": ""}
        for i, sig in enumerate(signals)
    ]
    return {"local_id": local_id, "pins": pins}


# ---------------------------------------------------------------------------
# Individual classifier rules
# ---------------------------------------------------------------------------


def test_jtag_connector_matched_by_signals() -> None:
    c = _connector("JTAG", ["VCC", "GND", "TCO/TDO", "TCI/TDI", "TMS", "TCK"])
    inf = infer_connector_metadata(c)
    assert "JTAG" in inf.description
    assert inf.symbol_hint == "Connector_Generic:Conn_01x06"


def test_usb_a_3_matched_on_ss_signals() -> None:
    c = _connector("J_USB1", ["VBUS", "D-", "D+", "GND", "SS_RX-", "SS_RX+", "GND", "SS_TX-", "SS_TX+"])
    inf = infer_connector_metadata(c)
    assert inf.description == "USB-A 3.0 receptacle"
    assert inf.symbol_hint == "Connector:USB3_A"


def test_ethernet_matched_on_tx_rx_pairs() -> None:
    c = _connector("J_ETH", ["TX+", "TX-", "RX+", "GND", "GND", "RX-", "GND", "GND"])
    inf = infer_connector_metadata(c)
    assert "Ethernet" in inf.description or "RJ45" in inf.description
    assert inf.symbol_hint == "Connector:RJ45"


def test_mini_pcie_matched_on_per_pairs() -> None:
    c = _connector(
        "J_HALOW",
        ["WAKE#", "3.3V", "COEX1", "GND", "COEX2", "1.5V", "CLKREQ#", "UIM_PWR",
         "GND", "UIM_DATA", "REFCLK-", "UIM_CLK", "REFCLK+", "UIM_RESET", "GND",
         "UIM_VPP", "Reserved", "GND", "Reserved", "W_DISABLE#", "GND", "PERST#",
         "PERn0", "3.3V", "PERp0", "GND", "GND", "1.5V", "GND", "SMB_CLK", "PETn0",
         "SMB_DATA", "PETp0", "GND", "GND", "USB_D-", "GND", "USB_D+", "3.3V",
         "GND", "3.3V", "LED_WWAN#", "GND", "LED_WLAN#", "Reserved", "LED_WPAN#",
         "Reserved", "1.5V", "Reserved", "GND", "Reserved", "3.3V"],
    )
    inf = infer_connector_metadata(c)
    assert "Mini-PCIe" in inf.description
    assert inf.symbol_hint == "Connector:Bus_PCI_Express_Mini"


def test_m2_cellular_matched_on_sim_signals() -> None:
    c = _connector(
        "J_CELL",
        ["GND", "3.3V", "GND", "3.3V", "GND", "Reserved"]  # base
        + ["SIM_VCC", "SIM_RST", "SIM_CLK", "SIM_DATA"]  # key signal set
        + ["PERST#"] * 40,  # pad to >40 pins
    )
    inf = infer_connector_metadata(c)
    assert "M.2" in inf.description and "Key-B" in inf.description
    assert inf.symbol_hint == "Connector:Bus_M.2_Socket_B"


def test_m2_wifi_matched_on_pcie_group_labels() -> None:
    c = _connector("J_WIFI", ["Power", "Key", "PCIe Rx", "PCIe Tx", "GND + Reserved"])
    inf = infer_connector_metadata(c)
    assert "M.2" in inf.description and "Key-E" in inf.description
    assert inf.symbol_hint == "Connector:Bus_M.2_Socket_E"


def test_barrel_jack_matched_on_tip_sleeve() -> None:
    c = _connector("J1", ["Sleeve", "Tip"])
    # Replace signals so GND is present (barrel rule requires both).
    c["pins"] = [
        {"pin": "1", "signal": "VIN", "function": "", "voltage": ""},
        {"pin": "2", "signal": "GND", "function": "", "voltage": ""},
    ]
    inf = infer_connector_metadata(c)
    assert "barrel jack" in inf.description.lower()
    assert inf.symbol_hint == "Connector:Barrel_Jack"


def test_usb_c_matched_on_cc_signals() -> None:
    c = _connector(
        "J_USB_C",
        ["GND", "TX1+/RX1-", "TX1-/RX1+", "VBUS", "CC1", "DP1/DN2", "DN1/DP2",
         "SBU1", "VBUS", "GND"] + ["CC2"] + ["dummy"] * 13,
    )
    inf = infer_connector_metadata(c)
    assert "USB Type-C" in inf.description or "USB-C" in inf.description


def test_generic_pin_header_is_fallback_for_40pin() -> None:
    """A 40-pin connector with nothing distinctive falls through to a plain header."""
    sigs = ["GND", "3.3V"] + [f"Generic_GPIO{i}" for i in range(38)]
    c = _connector("J2", sigs)
    inf = infer_connector_metadata(c)
    assert inf.symbol_hint == "Connector_Generic:Conn_01x40"
    assert "40-pin" in inf.description


def test_generic_pin_header_for_16pin() -> None:
    sigs = ["GND", "3.3V"] + [f"GNSS_{i}" for i in range(14)]
    c = _connector("J5", sigs)
    inf = infer_connector_metadata(c)
    assert inf.symbol_hint == "Connector_Generic:Conn_01x16"


# ---------------------------------------------------------------------------
# synthesize_bom_rows wrapper
# ---------------------------------------------------------------------------


def _minimal_design_artifact(connectors: list[dict]) -> dict:
    return {
        "project_id": "test",
        "schema_version": 1,
        "source_files": [],
        "components": [],
        "connectors": connectors,
        "subsystems": [],
        "raw_nets": [],
    }


def test_synthesize_skips_local_ids_in_existing_set() -> None:
    da = _minimal_design_artifact(
        [
            _connector("JTAG", ["TMS", "TCK", "TDI", "TDO", "VCC", "GND"]),
            _connector("J_USB_C", ["GND", "CC1", "CC2"] + ["x"] * 21),
        ]
    )
    rows = synthesize_bom_rows(da, existing_local_ids={"J_USB_C"})
    ids = {r["local_id"] for r in rows}
    assert "JTAG" in ids
    assert "J_USB_C" not in ids  # deduped


def test_synthesize_produces_bom_shaped_rows_with_required_fields() -> None:
    da = _minimal_design_artifact([_connector("J2", ["GND", "3.3V"] + ["gpio"] * 38)])
    rows = synthesize_bom_rows(da)
    assert len(rows) == 1
    r = rows[0]
    # Required bom.v1 fields.
    assert r["local_id"] == "J2"
    assert r["mpn"] == "Generic_PinHeader_40P"
    assert r["package"] == "PinHeader_40P"
    assert r["pin_count"] == 40
    assert r["role"] == "connector"
    assert 0 <= r["confidence"] <= 1
    assert r["symbol_hint"] == "Connector_Generic:Conn_01x40"


def test_synthesize_handles_empty_connectors_list() -> None:
    da = _minimal_design_artifact([])
    assert synthesize_bom_rows(da) == []
