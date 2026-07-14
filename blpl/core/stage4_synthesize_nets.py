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
    # Battery, system, and domain-qualified rails. Missing these is not cosmetic:
    # an unclassified rail lands in Default and gets a 0.2mm signal trace, so
    # dev.04's battery rail was being routed as if it carried a logic signal.
    (re.compile(r"^(VBAT|VSYS|VPH|VMOT|VBACKUP|VRTC|VSTORE)"), "Power_Bulk"),
    (re.compile(r"^(AVCC|DVCC|AVDD|DVDD|IOVDD|VDDIO)"), "Power_Bulk"),
    (re.compile(r"^(3V3|5V|12V|1V8|1V2|0V85|2V5)"), "Power_Bulk"),
    (re.compile(r"^\d+(\.\d+)?V(\b|$)"), "Power_Bulk"),  # "3.3V", "5V", "1.8V"
    (re.compile(r"^USB3_|^SS_[RT]X[+\-PN]?$"), "USB3_Diff_90Ohm"),
    (re.compile(r"^USB\d*_D[+\-PN]$"), "USB3_Diff_90Ohm"),
    (re.compile(r"^PCIE_"), "PCIe_Diff_85Ohm"),
    (re.compile(r"^DDR4_"), "DDR4_Diff_90Ohm"),
]


_DIFF_PAIR_SUFFIXES = {
    "P": "N",
    "N": "P",
    "DP": "DM",
    "DM": "DP",
    "TX_P": "TX_N",
    "TX_N": "TX_P",
    "RX_P": "RX_N",
    "RX_N": "RX_P",
    "+": "-",
    "-": "+",
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


def _diff_pair_complement(name: str) -> str | None:
    upper = name.upper()
    for suffix in _DIFF_PAIR_SUFFIX_ORDER:
        # Accept both _SUFFIX and SUFFIX (for + and -).
        if suffix in ("+", "-"):
            if upper.endswith(suffix):
                base = name[: -len(suffix)]
                return base + _DIFF_PAIR_SUFFIXES[suffix]
        else:
            sep_suffix = f"_{suffix}"
            if upper.endswith(sep_suffix):
                base = name[: -len(sep_suffix)]
                return base + "_" + _DIFF_PAIR_SUFFIXES[suffix]
    return None


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
        comp = _diff_pair_complement(name)
        if comp and comp in nets_by_name:
            nets_by_name[name]["diff_pair_of"] = comp

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
