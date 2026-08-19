"""Checking what happens where two boards plug together.

Within one board, Stage 4 turns connector pinouts into nets and Stage 7 runs DRC
over the result. Between boards, nothing looked at all: each board compiled and
passed on its own, and the pin-for-pin claim that the two would work when
plugged together was never tested. That claim is exactly where multi-board
designs go wrong — a connector pinout mirrored by hand, an SDA/SCL swap, a 5 V
rail meeting a 3V3 input — and it is the one class of error a per-board pipeline
structurally cannot catch.

What this does, per configuration: take each declared mate, line the two
connectors up pin by pin, and compare the signals. It reports disagreements
rather than resolving them, because the correct resolution is a decision about
the design and belongs to the person making it.

The findings, in descending order of how much they should worry you:

``signal_mismatch``
    Facing pins carry different signals. Either the pinout is wrong or the
    boards do not mate the way the manifest says. This is the one that puts
    smoke in the room.

``pin_count_mismatch``
    The connectors are different sizes. Sometimes deliberate — a 6-pin plug in
    a 8-pin shrouded header — so it is reported with both counts rather than
    assumed fatal.

``unmated_signal``
    A signal reaches a connector on one board and has nowhere to go on the
    other in this configuration. Expected for an optional board that is absent,
    which is why it is reported per configuration and not globally: a pin that
    dangles in `minimal` and connects in `full` is working as designed.

Mirroring is assumed only where the manifest says nothing. Two boards that mate
through a ribbon cable run pin 1 to pin 1; two that stack through a plug and
socket may reverse. ``order`` on a mate says which, and the default is straight
because that is what the overwhelming majority of board-to-board connectors do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .project_manifest import Configuration, Mate, ProjectManifest

# Signals that are expected to appear on both sides under different names, or to
# be absent without meaning anything is wrong.
_DONT_CARE = {"NC", "N/C", "DNC", "RESERVED", "KEY", ""}

# Signals worth a second look when they cross a board boundary.
#
# Deliberately *not* a prohibition. Carrying a controlled-impedance trace across
# a connector adds an impedance discontinuity, a return-path break, and a
# mechanical joint whose parasitics move with every mating cycle — so it earns
# scrutiny. It does not earn a refusal: plenty of designs do it on purpose and
# get it right, and an engineer who has decided to should not have to argue with
# their tools about it.
#
# So the default is a warning, and a project that wants it enforced says so:
#
#     ## Rules
#     - rf across boards: forbid
#
# That way the tool holds the standard its owner chose, rather than the one
# whoever wrote the checker happened to prefer.
_RF_MARKERS = (
    "RF", "ANT", "ANTENNA", "LNA", "PA_OUT", "BALUN", "COAX", "UFL", "IPEX", "SMA",
    "2G4", "5G8", "868M", "915M", "433M", "SUBGHZ", "GNSS_IN", "GPS_IN",
)


def is_rf(signal: str) -> bool:
    """Whether a signal name reads as radio-frequency.

    Name-based and therefore imperfect, which is the right trade here: a false
    positive costs one line in a report that a human dismisses in a second, and
    a false negative costs a board spin. Anything genuinely ambiguous should be
    renamed — a net whose name does not say it is RF is a problem on its own.
    """
    up = (signal or "").strip().upper()
    if not up:
        return False
    tokens = [t for t in re.split(r"[^A-Z0-9]+", up) if t]
    for m in _RF_MARKERS:
        for t in tokens:
            # Whole token, or the marker with a bare index after it. ANT1 and
            # ANT2 are the common way a second antenna gets named, and missing
            # them is the failure that costs a board spin — where a false
            # positive costs one line somebody dismisses.
            #
            # Digits only, deliberately: RFID_CS should not read as RF, and
            # allowing arbitrary trailing letters would make it.
            if t == m or re.fullmatch(rf"{re.escape(m)}\d+", t):
                return True
    return False


@dataclass
class Finding:
    kind: str
    severity: str          # "error" | "warning" | "info"
    configuration: str
    message: str
    mate: str = ""
    pin: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "configuration": self.configuration,
            "message": self.message,
            "mate": self.mate,
            "pin": self.pin,
        }


@dataclass
class CrossBoardReport:
    project_id: str
    findings: list[Finding] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)   # configuration names
    schema_version: int = 1

    @property
    def blocked(self) -> bool:
        return any(f.severity == "error" for f in self.findings)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "schema_version": self.schema_version,
            "blocked": self.blocked,
            "checked_configurations": self.checked,
            # Findings first: on a real project this file is mostly findings,
            # but the same reasoning as nets.v1 applies — whatever reads a
            # prefix of it must get the conclusions.
            "findings": [f.to_dict() for f in self.findings],
        }


def _norm(signal: str) -> str:
    return (signal or "").strip().upper()


def _pins_of(artifact: dict, connector: str) -> list[dict] | None:
    """A connector's pins from a board's design artifact, or None if absent."""
    for conn in artifact.get("connectors", []):
        if str(conn.get("local_id", "")).upper() == connector.upper():
            return list(conn.get("pins", []))
    return None


def _pin_key(pin: dict) -> str:
    return str(pin.get("pin", "")).strip()


def _facing(a_pins: list[dict], b_pins: list[dict], order: str) -> list[tuple[dict, dict | None]]:
    """Pair up pins across the mate.

    Straight pairs by pin label where both sides use the same labels, and falls
    back to position when they do not — a plug numbered 1..6 facing a socket
    numbered A1..A6 is a real thing, and refusing to check it would be worse
    than checking it positionally.
    """
    b_by_label = {_pin_key(p): p for p in b_pins}
    labels_align = all(_pin_key(p) in b_by_label for p in a_pins) and len(a_pins) == len(b_pins)
    seq = list(b_pins)
    if order == "reversed":
        seq = list(reversed(seq))
    out: list[tuple[dict, dict | None]] = []
    for i, a in enumerate(a_pins):
        if labels_align and order != "reversed":
            out.append((a, b_by_label.get(_pin_key(a))))
        else:
            out.append((a, seq[i] if i < len(seq) else None))
    return out


def check_mate(
    mate: Mate,
    a_artifact: dict,
    b_artifact: dict,
    configuration: str,
    *,
    order: str = "straight",
    rf_severity: str = "warning",
) -> list[Finding]:
    """Compare one pair of mating connectors, pin by pin."""
    label = f"{mate.a_board}.{mate.a_connector} <-> {mate.b_board}.{mate.b_connector}"
    a_pins = _pins_of(a_artifact, mate.a_connector)
    b_pins = _pins_of(b_artifact, mate.b_connector)

    if a_pins is None or b_pins is None:
        missing = mate.a_board if a_pins is None else mate.b_board
        conn = mate.a_connector if a_pins is None else mate.b_connector
        return [
            Finding(
                kind="missing_connector",
                severity="error",
                configuration=configuration,
                mate=label,
                message=(
                    f"{missing} declares no connector {conn}; the mate cannot be checked. "
                    "Either the pinout table is missing or the manifest names the wrong "
                    "connector."
                ),
            )
        ]

    out: list[Finding] = []
    if len(a_pins) != len(b_pins):
        out.append(
            Finding(
                kind="pin_count_mismatch",
                severity="warning",
                configuration=configuration,
                mate=label,
                message=(
                    f"{mate.a_board}.{mate.a_connector} has {len(a_pins)} pins, "
                    f"{mate.b_board}.{mate.b_connector} has {len(b_pins)}. Deliberate for a "
                    "keyed or shrouded pair; a mistake otherwise."
                ),
            )
        )

    for a, b in _facing(a_pins, b_pins, order):
        a_sig, a_pin = _norm(a.get("signal", "")), _pin_key(a)
        if b is None:
            if a_sig not in _DONT_CARE:
                out.append(
                    Finding(
                        kind="unmated_signal",
                        severity="warning",
                        configuration=configuration,
                        mate=label,
                        pin=a_pin,
                        message=(
                            f"{a_sig} on {mate.a_board}.{mate.a_connector} pin {a_pin} faces "
                            f"nothing on {mate.b_board}.{mate.b_connector}."
                        ),
                    )
                )
            continue
        b_sig = _norm(b.get("signal", ""))

        # Checked before the don't-care skip and before the match: an RF signal
        # crossing a connector is wrong even when both sides agree perfectly on
        # the name, so agreement must not be allowed to excuse it.
        for sig, board, conn in ((a_sig, mate.a_board, mate.a_connector),
                                 (b_sig, mate.b_board, mate.b_connector)):
            if is_rf(sig):
                forbidden = rf_severity == "error"
                out.append(
                    Finding(
                        kind="rf_crosses_boards",
                        severity=rf_severity,
                        configuration=configuration,
                        mate=label,
                        pin=a_pin,
                        message=(
                            f"{sig} on {board}.{conn} pin {a_pin} is a radio-frequency "
                            "signal crossing a board boundary."
                            + (
                                " This project forbids that."
                                if forbidden
                                else " That can be done well and is done deliberately all"
                                " the time; it just carries an impedance discontinuity and"
                                " a return-path break, so it wants more scrutiny than a"
                                " digital net would. Add 'rf across boards: forbid' under"
                                " ## Rules to make this an error."
                            )
                        ),
                    )
                )
                break

        if a_sig in _DONT_CARE or b_sig in _DONT_CARE:
            continue
        if a_sig != b_sig:
            out.append(
                Finding(
                    kind="signal_mismatch",
                    severity="error",
                    configuration=configuration,
                    mate=label,
                    pin=a_pin,
                    message=(
                        f"pin {a_pin} carries {a_sig} on {mate.a_board} but "
                        f"{_pin_key(b)} carries {b_sig} on {mate.b_board}. These pins face "
                        "each other when the boards are plugged together."
                    ),
                )
            )
    return out


def check(
    man: ProjectManifest,
    artifacts: dict[str, dict],
    *,
    configurations: Iterable[Configuration] | None = None,
    rf_severity: str | None = None,
) -> CrossBoardReport:
    """Check every declared mate in every configuration.

    ``artifacts`` maps board name to that board's Stage 0 design artifact. A
    board with no artifact is reported once rather than per mate — the useful
    message is "this board has not been built", not five copies of it.
    """
    report = CrossBoardReport(project_id=man.project_id)
    configs = list(configurations if configurations is not None else man.configurations)
    # The project's own standard wins; the caller can override for a one-off
    # check, and the default is advisory.
    severity = rf_severity or getattr(man, "rf_severity", None) or "warning"

    for cfg in configs:
        report.checked.append(cfg.name)
        present = set(cfg.boards)

        for name in sorted(present):
            if name not in artifacts:
                report.findings.append(
                    Finding(
                        kind="board_not_built",
                        severity="error",
                        configuration=cfg.name,
                        message=(
                            f"{name} is in configuration {cfg.name!r} but has no design "
                            "artifact — run Stage 0 for that board before checking mates."
                        ),
                    )
                )

        for mate in man.mates_for(present):
            a, b = artifacts.get(mate.a_board), artifacts.get(mate.b_board)
            if a is None or b is None:
                continue  # already reported as board_not_built
            report.findings.extend(
                check_mate(mate, a, b, cfg.name, rf_severity=severity)
            )

    return report
