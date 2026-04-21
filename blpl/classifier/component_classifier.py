"""Classify BOM rows into buckets the pipeline can auto-resolve.

Stage 3 calls ``classify(bom_row, symbols_root, footprints_root)`` for every
row. The return value says whether we can resolve the row's pin_map/symbol/
footprint without user interaction, and if so what to resolve it to.

Buckets:
  - ``passive_2pin``      — R, C, L, ferrite bead, LED, fuse. Identity pin_map {"1":"1","2":"2"}.
  - ``generic_connector`` — USB-A/B/C, barrel jack, FFC, M.2, mini-PCIe, plain headers.
                             Pin_map derived from the library symbol's pin-name table.
  - ``small_signal_3pin`` — SOT-23 MOSFET/BJT/LDO. Standard GDS-ish pin_map.
  - ``specific``          — everything else (FPGA, MCU, custom IC). No auto-resolution.

The classifier is deterministic: a small regex/lookup table. It does not call
an LLM — Stage 1's LLM is responsible for producing a useful BOM row
(description + package) in the first place. If a row slips through as
``specific`` when it shouldn't, add it to the lookup table rather than growing
heuristics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from blpl.emitter import loaders


Bucket = Literal["passive_2pin", "generic_connector", "small_signal_3pin", "specific"]


@dataclass
class ClassifierResult:
    bucket: Bucket
    lib_symbol: str | None = None
    lib_footprint: str | None = None
    pin_map: dict[str, str] | None = None
    source: str = "specific_needs_user"
    reason: str = ""
    # Non-fatal warnings (e.g. symbol loaded but no pin names) used for gap audit.
    warnings: list[str] = field(default_factory=list)

    @property
    def is_auto_resolved(self) -> bool:
        return self.bucket != "specific" and self.pin_map is not None


# ---------------------------------------------------------------------------
# Bucket 1: passives
# ---------------------------------------------------------------------------
#
# BOM rows for passives typically have a short MPN like "0603" / "100nF" and a
# description like "Resistor 10k 1% 0603". We match on the description first
# (most reliable), with MPN prefix + package as a fallback signal.

_PASSIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(resistor|chip resistor|thick film)\b", re.I), "Device:R"),
    (re.compile(r"\b(capacitor|mlcc|chip cap)\b", re.I), "Device:C"),
    (re.compile(r"\b(inductor|power inductor)\b", re.I), "Device:L"),
    (re.compile(r"\b(ferrite bead|ferrite)\b", re.I), "Device:FerriteBead"),
    (re.compile(r"\b(led|light.emitting diode)\b", re.I), "Device:LED"),
    (re.compile(r"\b(fuse|resettable fuse|polyfuse)\b", re.I), "Device:F_Fuse"),
]

# Default 2-pin SMD package footprints. Keyed on EIA size extracted from the
# BOM row's `package` column. If the size is unknown we pick 0603 — a
# middle-of-the-road choice that fits most hand-assembled designs.
_PASSIVE_FOOTPRINTS_BY_SIZE: dict[str, str] = {
    "0201": "Resistor_SMD:R_0201_0603Metric",
    "0402": "Resistor_SMD:R_0402_1005Metric",
    "0603": "Resistor_SMD:R_0603_1608Metric",
    "0805": "Resistor_SMD:R_0805_2012Metric",
    "1206": "Resistor_SMD:R_1206_3216Metric",
    "1210": "Resistor_SMD:R_1210_3225Metric",
}
_DEFAULT_PASSIVE_SIZE = "0603"


def _passive_footprint(lib_symbol: str, size: str) -> str:
    """Pick the right SMD package footprint based on the lib symbol and EIA size."""
    fp = _PASSIVE_FOOTPRINTS_BY_SIZE.get(size, _PASSIVE_FOOTPRINTS_BY_SIZE[_DEFAULT_PASSIVE_SIZE])
    # Translate the R_xxxx family name to the matching Capacitor/Inductor/LED name.
    if lib_symbol == "Device:C":
        return fp.replace("Resistor_SMD:R_", "Capacitor_SMD:C_")
    if lib_symbol == "Device:L":
        return fp.replace("Resistor_SMD:R_", "Inductor_SMD:L_")
    if lib_symbol == "Device:LED":
        return fp.replace("Resistor_SMD:R_", "LED_SMD:LED_")
    if lib_symbol == "Device:FerriteBead":
        return fp.replace("Resistor_SMD:R_", "Inductor_SMD:L_")
    if lib_symbol == "Device:F_Fuse":
        return fp.replace("Resistor_SMD:R_", "Fuse:Fuse_")
    return fp


_EIA_SIZE_RE = re.compile(r"\b(0201|0402|0603|0805|1206|1210|1812|2010|2512)\b")


def _extract_eia_size(row: dict) -> str:
    """Pull a 4-digit EIA package code from BOM row hints."""
    for key in ("package", "description", "mpn"):
        val = row.get(key) or ""
        m = _EIA_SIZE_RE.search(val)
        if m:
            return m.group(1)
    return _DEFAULT_PASSIVE_SIZE


def _classify_passive(row: dict) -> ClassifierResult | None:
    desc = (row.get("description") or "") + " " + (row.get("mpn") or "")
    for pattern, lib_symbol in _PASSIVE_PATTERNS:
        if pattern.search(desc):
            size = _extract_eia_size(row)
            return ClassifierResult(
                bucket="passive_2pin",
                lib_symbol=lib_symbol,
                lib_footprint=_passive_footprint(lib_symbol, size),
                pin_map={"1": "1", "2": "2"},
                source="passive_identity",
                reason=f"matched pattern {pattern.pattern!r}; package={size}",
            )
    return None


# ---------------------------------------------------------------------------
# Bucket 2: generic connectors (USB, barrel, FFC, M.2, mini-PCIe, plain headers)
# ---------------------------------------------------------------------------
#
# Each entry either names a fixed KiCad library symbol OR provides a factory
# returning the symbol name from BOM hints (used for pin-count-parameterised
# families like FFC/pin-header). The footprint default is the most common
# variant in the KiCad library; users can override per row via
# ``footprint_hint`` on the BOM.

@dataclass
class _ConnectorEntry:
    name: str
    pattern: re.Pattern[str]
    # Either lib_symbol (fixed) or symbol_for(row) -> str (parameterised).
    lib_symbol: str | None = None
    symbol_for: "callable | None" = None
    # Same options for the default footprint.
    lib_footprint: str | None = None
    footprint_for: "callable | None" = None


def _ffc_symbol_for(row: dict) -> str | None:
    pc = row.get("pin_count")
    if isinstance(pc, int) and 1 <= pc <= 99:
        # Connector_Generic stores Conn_01x01 through Conn_01x{N}; pad to two digits.
        return f"Connector_Generic:Conn_01x{pc:02d}"
    return None


def _ffc_footprint_for(row: dict) -> str | None:
    # Prefer the Amphenol F32Q horizontal series as a reasonable generic 0.5mm FFC.
    pc = row.get("pin_count")
    if isinstance(pc, int) and 4 <= pc <= 50:
        return f"Connector_FFC-FPC:Amphenol_F32Q-1A7x1-110{pc:02d}_1x{pc:02d}-1MP_P0.5mm_Horizontal"
    return None


def _pin_header_symbol_for(row: dict) -> str | None:
    pc = row.get("pin_count")
    desc = (row.get("description") or "").lower()
    if not isinstance(pc, int):
        return None
    if "2x" in desc or "dual row" in desc or "02x" in desc:
        # Even-count dual-row headers map to Conn_02x{N/2}.
        if pc % 2 == 0 and 2 <= pc // 2 <= 40:
            return f"Connector_Generic:Conn_02x{pc // 2:02d}_Odd_Even"
    if 1 <= pc <= 99:
        return f"Connector_Generic:Conn_01x{pc:02d}"
    return None


_CONNECTOR_TABLE: list[_ConnectorEntry] = [
    _ConnectorEntry(
        name="barrel_jack",
        pattern=re.compile(r"\bbarrel\s*jack\b|\bpower\s*jack\b|\bdc\s*jack\b", re.I),
        lib_symbol="Connector:Barrel_Jack",
        lib_footprint="Connector_BarrelJack:BarrelJack_CUI_PJ-063AH_Horizontal",
    ),
    # USB-C variants. Matching any of "USB-C", "USB type-C", "Type-C" — with
    # or without punctuation between "usb" and "c" — and prefer the most
    # specific variant (14P then 16P then full 24-pin). Recognises both
    # description-driven ("14-pin") and pin_count-driven routing (checked
    # after the table below).
    _ConnectorEntry(
        name="usb_c_receptacle_usb2_14p",
        pattern=re.compile(r"(usb.?c|type.?c).*receptacle.*(usb.?2|14.?p(?:in)?)", re.I),
        lib_symbol="Connector:USB_C_Receptacle_USB2.0_14P",
        lib_footprint="Connector_USB:USB_C_Receptacle_Amphenol_12401548E4-2A",
    ),
    _ConnectorEntry(
        name="usb_c_receptacle_usb2_16p",
        pattern=re.compile(r"(usb.?c|type.?c).*receptacle.*16.?p(?:in)?", re.I),
        lib_symbol="Connector:USB_C_Receptacle_USB2.0_16P",
        lib_footprint="Connector_USB:USB_C_Receptacle_Amphenol_12401548E4-2A",
    ),
    _ConnectorEntry(
        name="usb_c_receptacle_full",
        pattern=re.compile(r"(usb.?c|(usb\s*)?type.?c).*receptacle", re.I),
        lib_symbol="Connector:USB_C_Receptacle",
        lib_footprint="Connector_USB:USB_C_Receptacle_Amphenol_12401948E412A",
    ),
    _ConnectorEntry(
        name="usb_a",
        pattern=re.compile(r"\busb.?a\b.*(receptacle|jack|port)?", re.I),
        lib_symbol="Connector:USB_A",
        lib_footprint="Connector_USB:USB_A_Stewart_SS-52100-001_Horizontal",
    ),
    _ConnectorEntry(
        name="usb_b_micro",
        pattern=re.compile(r"usb.?(b.?)?micro|micro.?usb", re.I),
        lib_symbol="Connector:USB_B_Micro",
        lib_footprint="Connector_USB:USB_Micro-B_Molex-105017-0001",
    ),
    _ConnectorEntry(
        name="m2_2230_e",
        pattern=re.compile(r"m\.?2\s*(socket|slot|connector).*(key.?e|wifi|wireless).*(2230)", re.I),
        lib_symbol="Connector:Bus_M.2_Socket_E",
        lib_footprint="Connector_PCBEdge:M.2_2230-xx-E",
    ),
    _ConnectorEntry(
        name="m2_2280_m",
        pattern=re.compile(r"m\.?2\s*(socket|slot|connector).*(key.?m|nvme|ssd).*(2280)", re.I),
        lib_symbol="Connector:Bus_M.2_Socket_M",
        lib_footprint="Connector_PCBEdge:M.2_2280-xx-M",
    ),
    _ConnectorEntry(
        name="m2_2242_b",
        pattern=re.compile(r"m\.?2.*key.?b.*2242", re.I),
        lib_symbol="Connector:Bus_M.2_Socket_B",
        lib_footprint="Connector_PCBEdge:M.2_2242-xx-B",
    ),
    # Key-B / Key-E / Key-M fallbacks without a size hint. Pick a reasonable
    # default footprint by key — users can override in bom.json footprint_hint.
    _ConnectorEntry(
        name="m2_key_b_generic",
        pattern=re.compile(r"m\.?2.*key.?b\b", re.I),
        lib_symbol="Connector:Bus_M.2_Socket_B",
        lib_footprint="Connector_PCBEdge:M.2_3042-xx-B",
    ),
    _ConnectorEntry(
        name="m2_key_e_generic",
        pattern=re.compile(r"m\.?2.*key.?e\b", re.I),
        lib_symbol="Connector:Bus_M.2_Socket_E",
        lib_footprint="Connector_PCBEdge:M.2_2230-xx-E",
    ),
    _ConnectorEntry(
        name="m2_key_m_generic",
        pattern=re.compile(r"m\.?2.*key.?m\b", re.I),
        lib_symbol="Connector:Bus_M.2_Socket_M",
        lib_footprint="Connector_PCBEdge:M.2_2280-xx-M",
    ),
    _ConnectorEntry(
        name="rj45",
        pattern=re.compile(r"\brj.?45\b|ethernet\s*jack", re.I),
        lib_symbol="Connector:RJ45",
        # No universal footprint — leave blank so the user picks magnetics/LED variant.
        lib_footprint=None,
    ),
    _ConnectorEntry(
        name="mini_pcie",
        pattern=re.compile(r"mini.?pcie|mini.?pci.?express", re.I),
        lib_symbol="Connector:Bus_PCI_Express_Mini",
        lib_footprint="Connector_PCBEdge:BUS_PCI_Express_Mini_Full",
    ),
    _ConnectorEntry(
        name="pcie_x1",
        pattern=re.compile(r"pci.?e.*x1\b|pcie.*x1\b", re.I),
        lib_symbol="Connector:Bus_PCI_Express_x1",
        lib_footprint="Connector_PCBEdge:BUS_PCIexpress_x1",
    ),
    _ConnectorEntry(
        name="pcie_x4",
        pattern=re.compile(r"pci.?e.*x4\b|pcie.*x4\b", re.I),
        lib_symbol="Connector:Bus_PCI_Express_x4",
        lib_footprint="Connector_PCBEdge:BUS_PCIexpress_x4",
    ),
    _ConnectorEntry(
        name="ffc_fpc",
        pattern=re.compile(r"\bffc\b|\bfpc\b|flex.*cable.*connector", re.I),
        symbol_for=_ffc_symbol_for,
        footprint_for=_ffc_footprint_for,
    ),
    _ConnectorEntry(
        name="pin_header",
        pattern=re.compile(r"pin.?header|header.*pin|\b2\.54\s*mm\s*header|male.?header", re.I),
        symbol_for=_pin_header_symbol_for,
        footprint_for=None,  # User picks vertical/horizontal/through-hole variant.
    ),
]


def _pin_map_from_symbol(
    lib_symbol: str, symbols_root: Path, pin_count_hint: int | None = None
) -> tuple[dict[str, str], list[str]]:
    """Derive ``{signal_name: pin_number}`` from a library symbol's pin table.

    Returns ``(pin_map, warnings)``. Empty pin_map with a warning if the symbol
    loads but has no pin names (common for generic N-pin headers).
    """
    warnings: list[str] = []
    try:
        pins = loaders.load_symbol_pins(lib_symbol, symbols_root)
    except loaders.LibraryMiss as exc:
        warnings.append(f"library symbol not found: {exc}")
        return {}, warnings
    if not pins:
        warnings.append(f"no pins in {lib_symbol} unit 1")
        return {}, warnings

    pin_map: dict[str, str] = {}
    duplicate_names: dict[str, list[str]] = {}
    for pin in pins:
        number = pin.get("number") or ""
        name = (pin.get("name") or "").strip()
        if not number:
            continue
        if name and name != "~":
            # Library pin names like "~NC" (bar-over-NC) are tilde-escaped in .kicad_sym;
            # treat "~" as unconnected / anonymous and don't emit a mapping for them.
            if name in pin_map:
                # Multiple pins share this signal (e.g. GND at A1/A12/B1/B12 on
                # USB-C). Keep the first — downstream consumers can supplement
                # via an explicit pin_map override in bom.json.
                duplicate_names.setdefault(name, [pin_map[name]]).append(number)
            else:
                pin_map[name] = number
        else:
            # Generic header pin (no signal name). Fall back to identity so
            # downstream nets that reference pins by number still resolve.
            pin_map.setdefault(number, number)
    for name, nums in duplicate_names.items():
        warnings.append(
            f"signal {name!r} appears on multiple pins ({', '.join(nums)}); "
            f"kept {pin_map[name]}. Override in bom.json pin_map if another is preferred."
        )
    if pin_count_hint is not None and len(pin_map) < pin_count_hint:
        warnings.append(
            f"pin_count hint={pin_count_hint} but only {len(pin_map)} unique pins extracted"
        )
    return pin_map, warnings


def _classify_connector(row: dict, symbols_root: Path) -> ClassifierResult | None:
    text = " ".join(str(row.get(k) or "") for k in ("description", "mpn", "role", "package"))
    for entry in _CONNECTOR_TABLE:
        if not entry.pattern.search(text):
            continue
        lib_symbol = entry.lib_symbol or (entry.symbol_for(row) if entry.symbol_for else None)
        if not lib_symbol:
            # Parameterised family but couldn't pick a variant (missing pin_count).
            continue
        lib_footprint = entry.lib_footprint or (
            entry.footprint_for(row) if entry.footprint_for else None
        )
        pin_map, warns = _pin_map_from_symbol(
            lib_symbol, symbols_root, pin_count_hint=row.get("pin_count")
        )
        if not pin_map:
            # Couldn't derive a usable pin_map — leave it as specific so the
            # user gets an explicit prompt instead of a silent empty mapping.
            continue
        return ClassifierResult(
            bucket="generic_connector",
            lib_symbol=lib_symbol,
            lib_footprint=lib_footprint,
            pin_map=pin_map,
            source="connector_lookup",
            reason=f"matched connector entry {entry.name!r}",
            warnings=warns,
        )
    return None


# ---------------------------------------------------------------------------
# Bucket 3: small-signal 3-pin (SOT-23 MOSFETs, BJTs, LDOs)
# ---------------------------------------------------------------------------

_SMALL_SIGNAL_PATTERNS: list[tuple[re.Pattern[str], str, dict[str, str]]] = [
    # (pattern, lib_symbol, pin_map)
    # SOT-23 N-channel MOSFET: pin 1=G, 2=S, 3=D in the standard KiCad symbol.
    (
        re.compile(r"\b(n.?channel|nmos).*(mosfet|fet)\b|\bmosfet.*n.?channel\b|\b2n7002\b|\bbss13\d\b", re.I),
        "Device:Q_NMOS_GDS",
        {"G": "1", "S": "2", "D": "3"},
    ),
    (
        re.compile(r"\b(p.?channel|pmos).*(mosfet|fet)\b|\bmosfet.*p.?channel\b", re.I),
        "Device:Q_PMOS_GDS",
        {"G": "1", "S": "2", "D": "3"},
    ),
    (
        re.compile(r"\bnpn\s*(bipolar|bjt|transistor)?\b|\bbc[56][147]\d\b|\b2n390[46]\b", re.I),
        "Device:Q_NPN_BCE",
        {"B": "1", "C": "2", "E": "3"},
    ),
    (
        re.compile(r"\bpnp\s*(bipolar|bjt|transistor)?\b|\bbc[56][0-9]{2}\b", re.I),
        "Device:Q_PNP_BCE",
        {"B": "1", "C": "2", "E": "3"},
    ),
]


def _classify_small_signal_3pin(row: dict) -> ClassifierResult | None:
    desc = (row.get("description") or "") + " " + (row.get("mpn") or "")
    package = (row.get("package") or "").lower()
    pin_count = row.get("pin_count")
    if pin_count is not None and pin_count != 3:
        return None
    if "sot-23" not in package and "sot23" not in package and "sot-223" not in package:
        # Package hint required — SOT-23-6 MOSFETs have different pin_maps.
        return None
    for pattern, lib_symbol, pin_map in _SMALL_SIGNAL_PATTERNS:
        if pattern.search(desc):
            return ClassifierResult(
                bucket="small_signal_3pin",
                lib_symbol=lib_symbol,
                lib_footprint="Package_TO_SOT_SMD:SOT-23",
                pin_map=dict(pin_map),
                source="small_signal_3pin",
                reason=f"matched {pattern.pattern!r} + SOT-23 package",
            )
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def classify(
    row: dict,
    *,
    symbols_root: Path,
    footprints_root: Path | None = None,
) -> ClassifierResult:
    """Classify a BOM row. Always returns a ClassifierResult; ``bucket`` says whether it resolved."""
    for fn in (_classify_passive, _classify_small_signal_3pin):
        result = fn(row)
        if result is not None:
            return result
    result = _classify_connector(row, symbols_root)
    if result is not None:
        return result
    return ClassifierResult(
        bucket="specific",
        source="specific_needs_user",
        reason="no classifier bucket matched",
    )
