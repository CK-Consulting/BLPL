"""Stage 4: synthesize nets.v1 from a design_artifact.v1 + bom.v1.

Deterministic. Walks connector pinouts and optional component pin_maps to produce
nets keyed by signal name. Net-class assignment and differential-pair detection
are rule-based.

Signals such as "NC", "N/C", "-", "" are dropped (not-connected).
"""

from __future__ import annotations

import re
from pathlib import Path

from . import schema


# Order matters: first pattern that matches wins. Evaluated against UPPERCASE net name.
# Note: signal names that collide across subsystems (e.g. generic "TX+" on both J_ETH and J_USB1)
# are NOT disambiguated here — that is the LLM/user's responsibility in Stages 0/1. Stage 4 merges
# by literal signal name.
_CLASS_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(GND|AGND|DGND|PGND|VSS)\b"), "Power_Bulk"),
    (re.compile(r"^(VCC|VDD|V_|VIN|VBUS|PVDD)"), "Power_Bulk"),
    (re.compile(r"^(3V3|5V|12V|1V8|1V2|0V85|2V5)"), "Power_Bulk"),
    (re.compile(r"^\d+(\.\d+)?V(\b|$)"), "Power_Bulk"),  # "3.3V", "5V", "1.8V"
    (re.compile(r"^USB3_|^SS_[RT]X[+\-PN]?$"), "USB3_Diff_90Ohm"),
    (re.compile(r"^USB\d*_D[+\-PN]$"), "USB3_Diff_90Ohm"),
    (re.compile(r"^PCIE_"), "PCIe_Diff_85Ohm"),
    (re.compile(r"^DDR4_"), "DDR4_Diff_90Ohm"),
    # --- Additional controlled-impedance classes (dev.04 unified baseboard) ---
    # RGMII: source-synchronous Ethernet MAC↔PHY bus (e.g. SOM_RGMII_TXD0, ETH_RGMII_RXC).
    # Matched anywhere in the name because these signals are subsystem-prefixed. The class
    # is named RGMII_Diff per the project's net-class table; RGMII itself is single-ended,
    # so no diff-pair complement is expected. No dev.02/dev.03 net carries "RGMII", so this
    # rule only affects boards that declare the class.
    (re.compile(r".*RGMII"), "RGMII_Diff"),
    # RF 50Ω feed lines, e.g. GNSS_RF_IN (and any *_RF_OUT). The underscore boundary keeps
    # this off power nets like GNSS_VDD_RF. A bare RF_IN/RF_OUT is matched too.
    (re.compile(r"(?:.*_)?RF_(IN|OUT)$"), "RF_50Ohm"),
    # USB 2.0 D± pairs written with the _DP/_DN suffix AND a subsystem prefix, e.g.
    # SOM_USB_DP / SOM_USB_DN. The required leading "_" before USB means this does NOT
    # match the bare/anchored USB_DP or dev.02/dev.03's USB_D+/USB2_D+ forms, which stay
    # on the USB3_Diff_90Ohm rule above — so no existing board's nets are reclassified.
    (re.compile(r".*_USB\d*_D[PN]$"), "USB2_Diff_90Ohm"),
]


# Each suffix maps to the ordered candidate complements it may pair with. A suffix can
# have more than one valid complement spelling: USB 2.0 minus-side pins are written both
# as _DM (USB-IF) and _DN (used by dev.04's SOM_USB_DP/DN), so _DP must try both.
_DIFF_PAIR_SUFFIXES: dict[str, tuple[str, ...]] = {
    "P": ("N",),
    "N": ("P",),
    "DP": ("DM", "DN"),
    "DM": ("DP",),
    "DN": ("DP",),
    "TX_P": ("TX_N",),
    "TX_N": ("TX_P",),
    "RX_P": ("RX_N",),
    "RX_N": ("RX_P",),
    "+": ("-",),
    "-": ("+",),
}
# Longer suffixes must be tried first so e.g. TX_P is preferred over P.
_DIFF_PAIR_SUFFIX_ORDER = sorted(_DIFF_PAIR_SUFFIXES, key=len, reverse=True)


_NC_SIGNALS = {"NC", "N/C", "N.C.", "-", "", "DNC"}


def _normalize_signal(signal: str) -> str:
    s = signal.strip()
    # Drop trailing comments in parentheses.
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
    return s


def _assign_class(name: str) -> str:
    upper = name.upper()
    for pat, cls in _CLASS_RULES:
        if pat.match(upper):
            return cls
    return "Default"


def _diff_pair_complements(name: str) -> list[str]:
    """Candidate complement net names for a diff-pair half, best-spelling first.

    Returns [] if the name has no recognised diff-pair suffix. The caller keeps the
    first candidate that actually exists among the synthesized nets.
    """
    upper = name.upper()
    for suffix in _DIFF_PAIR_SUFFIX_ORDER:
        # Accept both _SUFFIX and SUFFIX (for + and -).
        if suffix in ("+", "-"):
            if upper.endswith(suffix):
                base = name[: -len(suffix)]
                return [base + comp for comp in _DIFF_PAIR_SUFFIXES[suffix]]
        else:
            sep_suffix = f"_{suffix}"
            if upper.endswith(sep_suffix):
                base = name[: -len(sep_suffix)]
                return [base + "_" + comp for comp in _DIFF_PAIR_SUFFIXES[suffix]]
    return []


def _resolve_refdes(local_id: str, bom: dict | None) -> str:
    if bom is None:
        return local_id
    for row in bom.get("rows", []):
        if row["local_id"] == local_id:
            return row.get("refdes") or row["local_id"]
    return local_id


def synthesize(design_artifact: dict, bom: dict | None = None) -> dict:
    """Build a nets.v1 dict from a design_artifact (and optional bom for refdes).

    Rules:
      - Each connector pin becomes a (refdes, pin) member of a net named after the signal.
      - Duplicate (refdes, pin) pairs within a net are deduped.
      - NC/ground/power signals still get nets (GND typically has many members).
      - Diff-pair partners get `diff_pair_of` set on both sides when both halves exist.
    """
    schema.validate("design_artifact", design_artifact)
    if bom is not None:
        schema.validate("bom", bom)

    nets_by_name: dict[str, dict] = {}

    for conn in design_artifact.get("connectors", []):
        refdes = _resolve_refdes(conn["local_id"], bom)
        src_ref = conn.get("source_ref")
        for pin in conn.get("pins", []):
            signal = _normalize_signal(pin["signal"])
            if signal.upper() in _NC_SIGNALS:
                continue
            net = nets_by_name.setdefault(
                signal,
                {
                    "name": signal,
                    "class": _assign_class(signal),
                    "members": [],
                    "source_refs": [],
                },
            )
            member = {"refdes": refdes, "pin": pin["pin"]}
            if member not in net["members"]:
                net["members"].append(member)
            if src_ref and src_ref not in net["source_refs"]:
                net["source_refs"].append(src_ref)

    # Honour explicit component pin_maps (non-connector components may declare nets too).
    # Currently design_artifact has no pin_map for non-connectors, so this is a no-op
    # placeholder for future extension.

    # Diff-pair detection (bidirectional tagging).
    for name in list(nets_by_name.keys()):
        for comp in _diff_pair_complements(name):
            if comp in nets_by_name:
                nets_by_name[name]["diff_pair_of"] = comp
                break

    out: dict = {
        "project_id": design_artifact["project_id"],
        "schema_version": 1,
        "nets": [_strip_empty(n) for n in nets_by_name.values()],
    }
    schema.validate("nets", out)
    return out


def _strip_empty(net: dict) -> dict:
    out = dict(net)
    if not out.get("source_refs"):
        out.pop("source_refs", None)
    return out


def run(
    design_artifact_path: Path,
    bom_path: Path | None,
    output_path: Path,
) -> dict:
    artifact = schema.load_json(design_artifact_path)
    bom = schema.load_json(bom_path) if bom_path and bom_path.exists() else None
    nets = synthesize(artifact, bom)
    schema.dump_json(output_path, nets)
    return nets
