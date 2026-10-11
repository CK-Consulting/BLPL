"""Synthesise BOM rows from ``design_artifact.connectors``.

Stage 0 parses two kinds of structured data from the markdown:

  1. ``components`` — BOM-worthy parts (ICs, modules, mechanicals).
  2. ``connectors`` — pinout tables: a ``local_id`` (J-prefixed refdes) and a
     list of ``(pin, signal, function, voltage)`` rows.

Stage 1's LLM only resolves category 1. The connectors never reach bom.json,
so Stage 2 has nothing to look up, Stage 3's classifier never classifies
them, and Stage 5 never emits them as components — they stay as
net-references only. This module closes the gap by walking ``connectors``
and producing one BOM row per entry, with a ``description`` + ``symbol_hint``
+ ``footprint_hint`` rich enough that Stage 3's classifier picks up the
pin_map automatically.

Deterministic, stdlib-only. The inference rules match by ``local_id`` and
signal-set patterns (e.g. presence of ``SS_RX+``/``SS_TX+`` → USB 3.0).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ConnectorInference:
    description: str
    mpn: str  # Placeholder MPN since connectors lack one in markdown.
    package: str
    symbol_hint: str | None = None
    footprint_hint: str | None = None
    confidence: float = 0.6
    notes: str = ""


def _signals(connector: dict) -> set[str]:
    """Return the set of unique signal names declared on a connector's pins."""
    return {
        str(p.get("signal", "")).strip()
        for p in connector.get("pins", [])
        if p.get("signal")
    }


def _signals_norm(connector: dict) -> set[str]:
    """Uppercase + whitespace-normalised signal set for case-insensitive matching."""
    return {s.upper().replace(" ", "").replace("#", "") for s in _signals(connector)}


# Ordered rules. First match wins. Each rule returns ``None`` when its
# signature doesn't apply, or a ``ConnectorInference`` otherwise.

def _jtag(connector: dict) -> ConnectorInference | None:
    local_id = connector["local_id"].upper()
    sigs = _signals_norm(connector)
    pc = len(connector.get("pins", []))
    if "JTAG" not in local_id and not (sigs & {"TMS", "TCK", "TDI", "TDO"}):
        return None
    return ConnectorInference(
        description=f"{pc}-pin ARM JTAG/SWD pin header",
        mpn=f"Generic_JTAG_{pc}P",
        package=f"PinHeader_{pc}P",
        symbol_hint=f"Connector_Generic:Conn_01x{pc:02d}",
        # 2.54mm vertical through-hole is the JTAG debug header default.
        footprint_hint=f"Connector_PinHeader_2.54mm:PinHeader_1x{pc:02d}_P2.54mm_Vertical",
        confidence=0.65,
        notes="Synthesized from JTAG signal signature (TMS/TCK/TDI/TDO).",
    )


def _usb_a_3(connector: dict) -> ConnectorInference | None:
    sigs = _signals_norm(connector)
    if not (sigs & {"SS_RX+", "SS_RX-", "SS_TX+", "SS_TX-", "SSRX+", "SSTX+"}):
        return None
    return ConnectorInference(
        description="USB-A 3.0 receptacle",
        mpn="Generic_USB3_A_Receptacle",
        package="USB-A",
        symbol_hint="Connector:USB3_A",
        confidence=0.7,
        notes="Synthesized from SuperSpeed USB signal signature (SS_RX/SS_TX).",
    )


def _usb_a_2(connector: dict) -> ConnectorInference | None:
    sigs = _signals_norm(connector)
    pc = len(connector.get("pins", []))
    if not (sigs & {"D+", "D-"}) or pc > 5:
        return None
    return ConnectorInference(
        description="USB-A 2.0 receptacle",
        mpn="Generic_USB2_A_Receptacle",
        package="USB-A",
        symbol_hint="Connector:USB_A",
        confidence=0.65,
        notes="Synthesized from USB 2.0 differential pair + pin count.",
    )


def _ethernet(connector: dict) -> ConnectorInference | None:
    sigs = _signals_norm(connector)
    if not ({"TX+", "TX-", "RX+", "RX-"} <= sigs):
        return None
    pc = len(connector.get("pins", []))
    return ConnectorInference(
        description=f"{pc}-pin RJ45 Ethernet jack",
        mpn="Generic_RJ45",
        package="RJ45",
        symbol_hint="Connector:RJ45",
        # No magnetics/LED baked in; users who need integrated magnetics override.
        footprint_hint="Connector_RJ:RJ45_Amphenol_RJHSE538X",
        confidence=0.6,
        notes="Synthesized from {TX+, TX-, RX+, RX-} signal signature.",
    )


def _mini_pcie(connector: dict) -> ConnectorInference | None:
    sigs = _signals_norm(connector)
    pcie_pairs = {"PERP0", "PERN0", "PETP0", "PETN0"}
    if not (sigs & pcie_pairs):
        return None
    return ConnectorInference(
        description="Mini-PCIe Full-size socket",
        mpn="Generic_Mini_PCIe_Socket",
        package="Mini-PCIe",
        symbol_hint="Connector:Bus_PCI_Express_Mini",
        footprint_hint="Connector_PCBEdge:BUS_PCI_Express_Mini_Full",
        confidence=0.7,
        notes="Synthesized from PCIe differential-pair signatures (PERp0/PERn0 or PETp0/PETn0).",
    )


def _m2_cellular(connector: dict) -> ConnectorInference | None:
    sigs = _signals_norm(connector)
    cell_markers = {"SIM_VCC", "SIM_CLK", "SIM_DATA", "SIM_RST", "UIM_PWR", "UIM_DATA"}
    pc = len(connector.get("pins", []))
    if not (sigs & cell_markers) or pc < 40:
        return None
    return ConnectorInference(
        description="M.2 Key-B socket for cellular NVMe (75-pin)",
        mpn="Generic_M2_Key_B_Socket",
        package="M.2_Key_B",
        symbol_hint="Connector:Bus_M.2_Socket_B",
        footprint_hint="Connector_PCBEdge:M.2_3042-xx-B",
        confidence=0.65,
        notes="Synthesized from SIM/UIM signal signature + high pin count.",
    )


def _m2_wifi(connector: dict) -> ConnectorInference | None:
    """Rough heuristic for WiFi/BT M.2 Key-E slots.

    The dev.03 markdown's J_WIFI table uses pin *groups* ('1-5', '6-9') rather
    than individual pins, so ``pin_count`` shows up as 8 even though the real
    socket has 75 contacts. Matching on signal-group labels handles both
    shapes — a real per-pin table and the grouped form.
    """
    sigs_raw = _signals(connector)  # case-preserved for the compound labels
    if not any(re.search(r"\bPCIe\s*(Rx|Tx)\b", s, re.I) for s in sigs_raw):
        return None
    return ConnectorInference(
        description="M.2 Key-E 2230 socket for WiFi/BT",
        mpn="Generic_M2_Key_E_Socket",
        package="M.2_Key_E_2230",
        symbol_hint="Connector:Bus_M.2_Socket_E",
        footprint_hint="Connector_PCBEdge:M.2_2230-xx-E",
        confidence=0.55,
        notes=(
            "Synthesized from 'PCIe Rx/Tx' signal-group labels. Pin count in the "
            "design_artifact is a group count, not the real 75-pin contact count — "
            "the real M.2 Key-E footprint still has 75 pads; user should review."
        ),
    )


def _barrel_jack(connector: dict) -> ConnectorInference | None:
    sigs = _signals_norm(connector)
    pc = len(connector.get("pins", []))
    # Classic barrel-jack signature: Tip/Sleeve, or 2-pin GND+VIN, or 3-pin with Ring.
    if pc not in (2, 3):
        return None
    if not (sigs & {"TIP", "SLEEVE", "RING", "VIN"} and "GND" in sigs):
        return None
    return ConnectorInference(
        description=f"{pc}-pin barrel jack power connector",
        mpn="Generic_BarrelJack",
        package="BarrelJack",
        symbol_hint="Connector:Barrel_Jack" if pc == 2 else "Connector:Barrel_Jack_Switch",
        footprint_hint="Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal",
        confidence=0.7,
        notes="Synthesized from Tip/Sleeve or GND+VIN 2-pin signature.",
    )


def _usb_c(connector: dict) -> ConnectorInference | None:
    """USB-C receptacle. Distinct enough signal set that we match even though
    J_USB_C usually also exists in the LLM-resolved components — dedup happens
    in the caller after synthesis."""
    sigs_norm = _signals_norm(connector)
    if not ("CC1" in sigs_norm and "CC2" in sigs_norm):
        return None
    pc = len(connector.get("pins", []))
    if pc >= 20:
        return ConnectorInference(
            description="USB Type-C receptacle (24-pin full)",
            mpn="Generic_USB_C_Receptacle",
            package="USB-C",
            symbol_hint="Connector:USB_C_Receptacle",
            confidence=0.7,
        )
    return ConnectorInference(
        description="USB Type-C receptacle USB2.0 14P",
        mpn="Generic_USB_C_USB2_Receptacle",
        package="USB-C_USB2",
        symbol_hint="Connector:USB_C_Receptacle_USB2.0_14P",
        confidence=0.65,
    )


def _generic_pin_header(connector: dict) -> ConnectorInference | None:
    """Fallback: unknown connector with a real pin count → generic N-pin header.

    Applies to J2/J3/J5-style expansion connectors when nothing more specific
    matched. Uses pin count to pick ``Conn_01xNN``.
    """
    pc = len(connector.get("pins", []))
    if not 1 <= pc <= 99:
        return None
    return ConnectorInference(
        description=f"{pc}-pin pin header",
        mpn=f"Generic_PinHeader_{pc}P",
        package=f"PinHeader_{pc}P",
        symbol_hint=f"Connector_Generic:Conn_01x{pc:02d}",
        footprint_hint=f"Connector_PinHeader_2.54mm:PinHeader_1x{pc:02d}_P2.54mm_Vertical",
        confidence=0.5,
        notes=(
            "Fallback synthesis: no distinguishing signal pattern matched, "
            "assumed a plain single-row 2.54mm-pitch vertical header."
        ),
    )


_RULES = (
    _jtag,
    _usb_a_3,
    _usb_a_2,
    _ethernet,
    _mini_pcie,
    _m2_cellular,
    _m2_wifi,
    _usb_c,
    _barrel_jack,
    _generic_pin_header,
)


def infer_connector_metadata(connector: dict) -> ConnectorInference:
    """Classify one ``design_artifact`` connector into a synthesized BOM row.

    Always returns a ``ConnectorInference`` — the final ``_generic_pin_header``
    rule is a catch-all that fires for any connector with a pin count ≥ 1.
    """
    for rule in _RULES:
        hit = rule(connector)
        if hit is not None:
            return hit
    return ConnectorInference(
        description="Unrecognised connector",
        mpn="Generic_Unknown_Connector",
        package="unknown",
        confidence=0.3,
        notes="No heuristic matched; user must set description/symbol_hint manually.",
    )


def synthesize_bom_rows(
    design_artifact: dict, *, existing_local_ids: set[str] | None = None
) -> list[dict]:
    """Build BOM rows for every connector not already in the existing set.

    The ``existing_local_ids`` set lets the caller dedupe against the
    LLM-resolved components — e.g. ``J_USB_C`` often appears both as a
    component (with MPN) and as a connector (with pinout). In that case the
    LLM-resolved row wins and we skip the synthesized duplicate.
    """
    existing = existing_local_ids or set()
    rows: list[dict] = []
    for conn in design_artifact.get("connectors", []) or []:
        local_id = conn["local_id"]
        if local_id in existing:
            continue
        inferred = infer_connector_metadata(conn)
        row: dict = {
            "local_id": local_id,
            "mpn": inferred.mpn,
            "package": inferred.package,
            "pin_count": len(conn.get("pins", [])),
            "description": inferred.description,
            "role": "connector",
            "confidence": inferred.confidence,
        }
        if inferred.symbol_hint:
            row["symbol_hint"] = inferred.symbol_hint
        if inferred.footprint_hint:
            row["footprint_hint"] = inferred.footprint_hint
        if inferred.notes:
            row["notes"] = inferred.notes
        rows.append(row)
    return rows
