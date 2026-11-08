"""Findings a project accepts on purpose, each with its reason.

Some findings are true and intended: verdacor-ear-tag's battery contacts reach
past the board edge because the enclosure puts the cell there, and its NFC
wire pads sit at the edge so the antenna's wires can be soldered to them.
Reported as errors on every run, they bury the findings that matter, and a
board that is ready reads as one that is not. Silencing them would be worse:
nobody could see what was waived or why.

project.yaml ``review.accept`` names them::

    review:
      accept:
        - rule: off_board            # placement audit; or a Stage 8 rule id
          parts: [BT_POS, BT_NEG]
          reason: Contacts reach past the edge into the cell's cavity (enclosure).

An accepted finding is not removed. It moves to an "accepted" list with its
reason, in the placement audit and in Stage 8, so the record says what was
waived, by which entry, and why. ``rule`` is a glob (``PM-*``). With ``parts``
the entry covers a finding only when every part the finding names is listed;
with ``nets`` likewise for nets; with neither it covers the rule outright.

Stage 7 applies the same entries to kicad-cli's ERC and DRC violations, named
``erc:<type>`` and ``drc:<type>`` after the violation's ``type``
(``erc:pin_to_pin``, ``drc:clearance``; ``erc:*`` is a glob like any other).
The prefix keeps them apart from Stage 8 ids and placement findings, and
Stage 7 considers *only* entries whose rule starts with it, so a broad glob
meant for another stage cannot waive an ERC or DRC finding::

    review:
      accept:
        - rule: erc:pin_to_pin
          parts: [U6]
          reason: BMI323 SDO is strapped to GND on purpose to set its I2C address.

An ERC finding's parts are the symbols its items name, less the ``#FLG`` and
``#PWR`` symbols the emitter generates (their numbering moves between runs,
so ``parts`` never needs them). It has no nets. A DRC finding's parts are the
footprints its items name and its nets the nets on its copper items. DRC
unconnected items and schematic-parity findings are never waivable.
"""

from __future__ import annotations

import fnmatch
from typing import Iterable


def entries(config: dict | None) -> list[dict]:
    """The accept entries, normalised. Empty when the project accepts nothing."""
    out = []
    for e in ((config or {}).get("review") or {}).get("accept") or []:
        if not isinstance(e, dict) or not e.get("rule") or not e.get("reason"):
            continue
        out.append({
            "rule": str(e["rule"]),
            "parts": set(e.get("parts") or []) or None,
            "nets": set(e.get("nets") or []) or None,
            "reason": str(e["reason"]),
        })
    return out


def reason_for(rule: str, parts: Iterable[str], nets: Iterable[str], accepted: list[dict]) -> str | None:
    """The reason an entry gives for this finding, or None when none covers it."""
    parts, nets = set(parts or ()), set(nets or ())
    for e in accepted:
        if not fnmatch.fnmatchcase(rule or "", e["rule"]):
            continue
        if e["parts"] is not None and (not parts or not parts <= e["parts"]):
            continue
        if e["nets"] is not None and (not nets or not nets <= e["nets"]):
            continue
        return e["reason"]
    return None


def split(findings: list[dict], accepted: list[dict], *, rule_key: str = "rule_id",
          parts_key: str = "components", nets_key: str = "nets") -> tuple[list[dict], list[dict]]:
    """(still open, accepted with their ``accepted_reason``)."""
    if not accepted:
        return list(findings), []
    open_, waived = [], []
    for f in findings:
        parts = f.get(parts_key)
        parts = [parts] if isinstance(parts, str) else (parts or [])
        why = reason_for(str(f.get(rule_key) or ""), parts, f.get(nets_key) or [], accepted)
        if why is None:
            open_.append(f)
        else:
            waived.append({**f, "accepted_reason": why})
    return open_, waived
