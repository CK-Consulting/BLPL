"""Stage 4: synthesize nets.v1 from a design_artifact.v1 + bom.v1.

Deterministic. Walks connector pinouts and optional component pin_maps to produce
nets keyed by signal name. Net-class assignment and differential-pair detection
are rule-based.

Signals such as "NC", "N/C", "-", "" are dropped (not-connected).
"""

from __future__ import annotations

import re
import sys
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
    # Subsystem-prefixed power rails. The anchored rules above only catch bare rail
    # names; a rail routed through a connector is usually prefixed with its subsystem
    # (SOM_VIN, ETH_PWR_OUT, GNSS_VBCKP, CELL_USB_VBUS) and would otherwise fall to
    # Default. Match a curated set of unambiguous power tokens as the trailing _-word.
    # Deliberately does NOT match enable/control lines (*_PWR_EN) — those are GPIO, not
    # rails — nor *_VDD_RF, an internally-generated RF supply the design routes locally.
    (re.compile(r".*_(VIN|VBUS|VBCKP|VBACKUP|PWR_IN|PWR_OUT)$"), "Power_Bulk"),
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


def is_no_connect(signal: str) -> bool:
    """Whether a pinout row declares its pin deliberately unconnected.

    The bare spellings in ``_NC_SIGNALS``, the ``NC_<part>_<n>`` form that
    doctor tells people to write so two unconnected pins on one part never
    collapse into a single "NC" net, and the datasheet word "Reserved". The
    docs have always said all three are dropped; the code only dropped the
    bare set, so every ``NC_CELL_81`` became a one-pin net — 27 of them on one
    board — and thirty "Reserved" pins would have shorted into one net. The
    schematic emitter had no way to tell a pin the designer left open from one
    the design forgot. Stage 5 imports this predicate to decide which pins get
    a no-connect marker, so the two stages cannot disagree about what "open"
    means. Doctor still reports "Reserved" (DOC-004) because on some parts it
    means "future function", and a name that says which is better.
    """
    up = (signal or "").strip().upper()
    return up in _NC_SIGNALS or up.startswith("NC_") or up == "RESERVED"


# --- GPIO-map consumption ----------------------------------------------------
#
# A GPIO-assignment map ("GPIO | Signal | Destination | ...") carries the HOST
# half of every control net. On dev.04 the host (an ESP32-S3 module) has no
# pinout table — its connectivity exists only in this map — so before it was
# consumed, every enable/reset/chip-select net ended at the peripheral and the
# host side simply didn't exist.

# A pin cell that can actually bind: one token ending in digits (GPIO8, IO42).
# "GPIO— (TBD)" and "GPIO11-18 (subset)" fail — they are counted, not guessed at.
_GPIO_PIN_RE = re.compile(r"[A-Za-z_]*\d+\Z")

# What a datasheet calls a pin before the design gives it a job: STM32 ports
# (PB6), nRF ports (P0.04, P1_15), ESP/generic GPIO numbers (GPIO12, IO42). A
# pinout table that lists these as the "signal" is stating the silicon's name
# for the ball, not a net — which is why the GPIO map is allowed to move such a
# ball onto a real net, and not allowed to move one the pinout already put on
# SPI_CS.
_PORT_NAME_RE = re.compile(r"^(P[A-Z]\d{1,2}|P\d+[._]\d+|GPIO\d+|IO\d+)$", re.IGNORECASE)

# A signal cell that names ONE net. Multi-signal shorthand ("CAM_PCLK, CAM_HSYNC",
# "SOM_SPI_*", "BLE_B_UART_TX/RX", "Boot strap") is reported, never split by guess.
_ACTIONABLE_SIGNAL_RE = re.compile(r"[A-Za-z0-9_+\-]+\Z")

# Destination cross-references: "J_BLE_A pin 7", "U1 pin 36", "J_CAM pins 5-12",
# "J_SOM pins 12, 15". Only the "<refdes> pin(s) <numbers>" shape is machine-read;
# prose destinations ("Audio-haptics subsystem") deliberately parse to nothing.
_DEST_RE = re.compile(r"\b([JU][A-Za-z0-9_]*)\s+pins?\s+(\d+(?:\s*[-–]\s*\d+)?(?:\s*,\s*\d+)*)")


def _parse_destinations(dest: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for refdes, pins in _DEST_RE.findall(dest or ""):
        rng = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", pins.strip())
        if rng:
            for p in range(int(rng.group(1)), int(rng.group(2)) + 1):
                out.append((refdes, str(p)))
        else:
            for p in re.split(r"[,\s]+", pins.strip()):
                if p:
                    out.append((refdes, p))
    return out


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
            if is_no_connect(signal):
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

    warnings = _apply_gpio_assignments(design_artifact, nets_by_name, bom)

    # A host pin the pinout table names after itself (PB0, GPIO12) and nothing
    # else touches is an unassigned pin, not a one-pin net called PB0. Left
    # in, a 216-ball MCU contributes a hundred and fifty "single-pin net"
    # findings that say nothing the GPIO map's TBD count has not already said,
    # and drown the nets that really do reach only one pin. Dropped here and
    # counted once; Stage 5 gives each pin a no-connect flag.
    unassigned: list[str] = []
    for name in list(nets_by_name):
        net = nets_by_name[name]
        if len(net["members"]) == 1 and _PORT_NAME_RE.match(name):
            m = net["members"][0]
            unassigned.append(f"{m['refdes']} {m['pin']} ({name})")
            del nets_by_name[name]
    if unassigned:
        warnings.append(
            {
                "code": "STAGE4-006",
                "summary": (
                    f"{len(unassigned)} host pin(s) named only by their port have no "
                    f"assignment and are left unconnected: {', '.join(unassigned[:12])}"
                    + (f", … (+{len(unassigned) - 12} more)" if len(unassigned) > 12 else "")
                    + "."
                ),
                "fix": (
                    "Nothing, for a pin that is meant to stay free. Give a pin a job by "
                    "listing it in the host's GPIO map or naming its signal in the pinout."
                ),
                "unassigned_pins": [u.split(" (")[0] for u in unassigned],
            }
        )

    # Diff-pair detection (bidirectional tagging).
    for name in list(nets_by_name.keys()):
        for comp in _diff_pair_complements(name):
            if comp in nets_by_name:
                nets_by_name[name]["diff_pair_of"] = comp
                break

    out: dict = {"project_id": design_artifact["project_id"], "schema_version": 1}
    # Warnings before nets, deliberately. On a real board the net list runs to
    # tens of thousands of characters and the findings are a handful; anything
    # that reads a prefix of this file — a log tail, a context window — must get
    # the findings, not lose them behind the data they are about.
    if warnings:
        out["warnings"] = warnings
    out["nets"] = [_strip_empty(n) for n in nets_by_name.values()]
    schema.validate("nets", out)
    return out


def _apply_gpio_assignments(
    design_artifact: dict, nets_by_name: dict[str, dict], bom: dict | None
) -> list[dict]:
    """Fold GPIO-map rows into the synthesized nets. Returns warnings.

    Per row, in order of preference:
      - the named signal already has a net → the host pin joins it;
      - the signal is unknown but every machine-readable destination pin sits on
        ONE existing net → the wire clearly exists under another name: the host
        pin joins that net AND the naming split is reported (STAGE4-002). On a
        real design this was five charger-control nets that would otherwise have
        shipped split in two, with the battery-chemistry straps reaching nothing;
      - destinations land on SEVERAL nets → grouped-row shorthand; reported, and
        nothing is guessed;
      - no destination resolves → a net is created holding just the host pin,
        and its dangling other end is reported (STAGE4-004).
    Rows whose pin or signal cell can't bind ("GPIO— (TBD)", "CAM_PCLK, CAM_HSYNC")
    are counted in one summary warning (STAGE4-001) — the same no-silent-drops
    contract Stage 0's warnings carry.
    """
    assignments = design_artifact.get("gpio_assignments", [])
    if not assignments:
        return []

    warnings: list[dict] = []
    unbound: list[str] = []

    # (refdes, pin) -> owning net name, from the pinout-derived nets.
    member_owner: dict[tuple[str, str], str] = {}
    for net in nets_by_name.values():
        for m in net["members"]:
            member_owner.setdefault((m["refdes"], m["pin"]), net["name"])

    # The host's own pinout, when it has one: normalised signal -> physical pin,
    # and the set of physical pins. dev.04's host had no pinout table, so the
    # GPIO map was its only connectivity and the GPIO cell was the pin. An MCU
    # with a 216-ball pinout table is the other case: ball A4 is already on a
    # one-pin net called PB6, and the map's "PB6 carries I2C_SCL" has to move
    # that ball onto I2C_SCL — not add a second, logical member that the
    # emitter resolves to the same ball and KiCad reads as a short between the
    # two nets.
    host_pin_by_signal: dict[str, dict[str, str]] = {}
    host_signal_by_pin: dict[str, dict[str, str]] = {}
    for conn in design_artifact.get("connectors", []):
        ref = _resolve_refdes(conn["local_id"], bom)
        for pin in conn.get("pins", []):
            phys = str(pin.get("pin") or "").strip()
            sig = _normalize_signal(str(pin.get("signal") or ""))
            if not phys:
                continue
            host_signal_by_pin.setdefault(ref, {})[phys] = sig
            if sig:
                host_pin_by_signal.setdefault(ref, {}).setdefault(sig, phys)

    def _physical(host: str, gpio: str) -> str:
        """The host pin a GPIO cell names: by pinout signal, else itself (a ball,
        or — with no pinout table — the only name the pin has)."""
        return host_pin_by_signal.get(host, {}).get(gpio, gpio)

    for a in assignments:
        signal = _normalize_signal(a["signal"])
        if is_no_connect(signal):
            continue
        src_ref = a.get("source_ref")
        gpio = a["gpio"].strip()
        host = _resolve_refdes(a["host"], bom)
        bindable = bool(_GPIO_PIN_RE.fullmatch(gpio))

        if not _ACTIONABLE_SIGNAL_RE.fullmatch(signal):
            unbound.append(f"{a['signal']} ({gpio or 'no pin'})")
            continue

        # Where do the row's named destination pins actually sit?
        dest_owners: dict[str, list[str]] = {}
        dest_missing: list[str] = []
        for refdes, pin in _parse_destinations(a.get("destination", "")):
            owner = member_owner.get((_resolve_refdes(refdes, bom), pin))
            if owner is None:
                dest_missing.append(f"{refdes} pin {pin}")
            else:
                dest_owners.setdefault(owner, []).append(f"{refdes} pin {pin}")

        mismatched = {o: pins for o, pins in dest_owners.items() if o != signal}
        target: str | None = None

        if signal in nets_by_name:
            target = signal
            if mismatched:
                listed = "; ".join(f"{o} (at {', '.join(p)})" for o, p in mismatched.items())
                warnings.append(
                    {
                        "code": "STAGE4-002",
                        "summary": (
                            f"GPIO map routes {signal} to pins the pinouts place on "
                            f"other net(s): {listed}."
                        ),
                        "fix": "Make the GPIO map and the pinout tables agree on one name per wire.",
                        "net": signal,
                        **({"source_ref": src_ref} if src_ref else {}),
                    }
                )
        elif len(dest_owners) == 1:
            # The wire exists — under a different name. Join it, loudly.
            target = next(iter(dest_owners))
            pins = ", ".join(dest_owners[target])
            warnings.append(
                {
                    "code": "STAGE4-002",
                    "summary": (
                        f"GPIO map names the signal {signal}, but the pinout net at "
                        f"{pins} is named {target}"
                        + (
                            f" — joined {host} {gpio} to {target}; rename one side."
                            if bindable
                            else " — and the row's pin cell cannot bind, so nothing was joined."
                        )
                    ),
                    "fix": "Use one name per wire across the GPIO map and the pinout tables.",
                    "net": target,
                    **({"source_ref": src_ref} if src_ref else {}),
                }
            )
        elif len(dest_owners) > 1:
            listed = "; ".join(f"{o} (at {', '.join(p)})" for o, p in dest_owners.items())
            warnings.append(
                {
                    "code": "STAGE4-002",
                    "summary": (
                        f"GPIO map row {signal} spans {len(dest_owners)} pinout nets: "
                        f"{listed}. Grouped rows cannot bind — one row per signal."
                    ),
                    "fix": "Split the row so each signal has its own GPIO and destination.",
                    **({"source_ref": src_ref} if src_ref else {}),
                }
            )
        elif bindable:
            # No resolvable destination: the host pin is all we know. Keep the
            # net (it is real design intent) and say the other end is dangling.
            nets_by_name[signal] = {
                "name": signal,
                "class": _assign_class(signal),
                "members": [],
                "source_refs": [],
            }
            target = signal
            dangling = a.get("destination", "").strip() or "(none given)"
            warnings.append(
                {
                    "code": "STAGE4-004",
                    "summary": (
                        f"Net {signal} created from the GPIO map with only the host pin "
                        f"{host} {gpio}; its destination ‘{dangling}’ does not "
                        "resolve to a refdes+pin."
                    ),
                    "fix": (
                        "Write the destination as '<refdes> pin <n>' or add the signal to "
                        "that part's pinout table."
                    ),
                    "net": signal,
                    **({"source_ref": src_ref} if src_ref else {}),
                }
            )

        if dest_missing:
            warnings.append(
                {
                    "code": "STAGE4-003",
                    "summary": (
                        f"GPIO map row {signal} names destination pin(s) no pinout "
                        f"declares: {', '.join(dest_missing)}."
                    ),
                    "fix": "Add the pin to that part's pinout table, or fix the reference.",
                    **({"source_ref": src_ref} if src_ref else {}),
                }
            )

        if not bindable:
            unbound.append(f"{a['signal']} ({gpio or 'no pin'})")
            continue

        if target is not None:
            net = nets_by_name[target]
            phys = _physical(host, gpio)
            current = member_owner.get((host, phys))
            if current is not None and current != target:
                own = host_signal_by_pin.get(host, {}).get(phys)
                names_port = current == own and (current == gpio or _PORT_NAME_RE.match(current))
                if names_port and len(nets_by_name[current]["members"]) == 1:
                    # The pinout table named this ball after its own port pin
                    # (PB6) and nothing else is on that net: the map is the
                    # more specific statement, so the ball moves.
                    del nets_by_name[current]
                    member_owner.pop((host, phys), None)
                else:
                    warnings.append(
                        {
                            "code": "STAGE4-005",
                            "summary": (
                                f"GPIO map puts {host} {gpio} on {signal}, but the pinout "
                                f"table already places that pin ({phys}) on {current}. "
                                "One pin, two nets — nothing was joined."
                            ),
                            "fix": (
                                "Make the pinout table and the GPIO map agree: name the pin "
                                "one thing, or drop it from one of the two tables."
                            ),
                            "net": signal,
                            **({"source_ref": src_ref} if src_ref else {}),
                        }
                    )
                    unbound.append(f"{a['signal']} ({gpio}: conflicts with {current})")
                    continue
            member = {"refdes": host, "pin": phys}
            if member not in net["members"]:
                net["members"].append(member)
            if src_ref:
                refs = net.setdefault("source_refs", [])
                if src_ref not in refs:
                    refs.append(src_ref)
            member_owner[(host, phys)] = target

    if unbound:
        warnings.append(
            {
                "code": "STAGE4-001",
                "summary": (
                    f"{len(unbound)} GPIO map row(s) could not bind — pin is TBD/a range, "
                    f"or the signal cell names more than one net: {'; '.join(unbound)}."
                ),
                "fix": (
                    "One row per signal, with a concrete pin (GPIO8). TBD rows are fine "
                    "to keep — they are counted here, never silently dropped."
                ),
            }
        )
    return warnings


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

    # Echo warnings to stderr, same contract as Stage 0: a warning recorded only
    # in the JSON looks exactly like success from the terminal.
    for w in nets.get("warnings", []):
        where = w.get("source_ref") or {}
        loc = where.get("file", "")
        if where.get("line_start"):
            loc = f"{loc}:{where['line_start']}"
        print(f"stage4: warning [{w['code']}] {loc}: {w['summary']}", file=sys.stderr)
        if w.get("fix"):
            print(f"        fix: {w['fix']}", file=sys.stderr)
    return nets
