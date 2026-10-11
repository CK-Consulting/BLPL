"""Explicit design-doc pins, applied wherever BOM rows are read.

Stage 1 pins explicit references past its LLM, and that is the right place to
pin — but Stage 1 is the LLM stage, so routing every pin exclusively through
it prices a deterministic fact at LLM cost: edit a Symbol cell in the design
doc, and the change reaches nothing until a full Stage 1 rerun regenerates
bom.json. In practice that meant a user added pins to the one table they can
actually edit, reran the deterministic stages, and watched Stage 5 keep
resolving from the stale hints — the doc said one thing, every artifact said
another, and the only bridge between them was a paid model call.

So the pins are also applied at read time. Stage 2 and Stage 5 overlay the
deterministic artifact's explicit references onto the BOM rows they load:
an explicit ``Lib:Name`` in the Symbol or Package column, and the not_placed
flag, all with the same authority they'd have coming out of Stage 1. Bare
hints ("0402", "Module") are untouched — canonicalising those is the LLM's
job, and this module has no opinion about them. bom.json on disk is not
rewritten; the overlay is how the stages *read* it, so a later Stage 1 rerun
produces the same result this shortcut does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_ARTIFACT = Path(".pipeline") / "design_artifact.deterministic.json"


def _artifact(project_dir: Path, board: str | None) -> Path:
    """The deterministic Stage 0 artifact for this board.

    Board-qualified like every other per-board artifact. Reading the
    unqualified name on a multi-board project found nothing, so the overlay
    silently did not apply and every doc-stated pin went unread.
    """
    if board is None:
        return Path(project_dir) / _ARTIFACT
    from .project_manifest import artifact_path

    return artifact_path(Path(project_dir), "design_artifact.deterministic", board=board)


def load(project_dir: Path, board: str | None = None) -> dict[str, dict[str, Any]]:
    """local_id -> the explicit pins the design doc states for it.

    Reads the deterministic Stage 0 artifact — the one place the doc's tables
    land without a model in between. Absent or unreadable means no pins: the
    overlay must never be the thing that breaks a run.
    """
    path = _artifact(project_dir, board)
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    from .symbol_resolution import is_not_placed

    pins: dict[str, dict[str, Any]] = {}
    for comp in artifact.get("components", []):
        entry: dict[str, Any] = {}
        if ":" in (comp.get("symbol_hint") or ""):
            entry["symbol_hint"] = comp["symbol_hint"]
        if ":" in (comp.get("package_hint") or ""):
            entry["footprint_hint"] = comp["package_hint"]
        if is_not_placed(comp.get("package_hint")):
            entry["not_placed"] = True
        if entry:
            pins[comp["local_id"]] = entry
    return pins


def pin_maps(project_dir: Path, board: str | None = None) -> dict[str, dict[str, str]]:
    """local_id -> {signal: pin} derived from the doc's pinout tables.

    The doc's pinout tables are the durable statement of signal→pin — the
    thing DOC-011 tells people to write, the thing datasheet extraction
    lands in. But the pin_map a run actually uses lived only in bom.json,
    put there by resolve-pin-map calls and classifier write-backs — and
    bom.json is Stage 1's output, so one Stage 1 rerun silently destroyed
    every accumulated pin_map and Stage 3 asked for 48 of them again by
    hand. Deriving from the artifact makes the doc the source: what a rerun
    wipes, the next Stage 3 re-derives.

    A signal on several pins (GND, VDD) keeps the last pin listed — the same
    collapse the CSV path has always performed; Stage 4 wires multi-pin nets
    from the pinout tables themselves, not from this map.
    """
    path = _artifact(project_dir, board)
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, dict[str, str]] = {}
    for conn in artifact.get("connectors", []):
        pin_map: dict[str, str] = {}
        for p in conn.get("pins") or []:
            signal = str(p.get("signal") or "").strip()
            pin = str(p.get("pin") or "").strip()
            if signal and pin:
                pin_map[signal] = pin
        if pin_map:
            out[conn["local_id"]] = pin_map
    return out


def apply(rows: list[dict], project_dir: Path | None, board: str | None = None) -> int:
    """Overlay the doc's explicit pins onto BOM rows, in place.

    Returns how many rows changed. Mirrors exactly what Stage 1's pinning
    writes, so reading a stale bom.json through this overlay equals reading a
    fresh one.
    """
    if project_dir is None:
        return 0
    pins = load(Path(project_dir), board)
    if not pins:
        return 0
    changed = 0
    for r in rows:
        pin = pins.get(r.get("local_id"))
        if not pin:
            continue
        before = (r.get("symbol_hint"), r.get("footprint_hint"), r.get("package"))
        if "symbol_hint" in pin:
            r["symbol_hint"] = pin["symbol_hint"]
        if "footprint_hint" in pin:
            r["footprint_hint"] = pin["footprint_hint"]
        if pin.get("not_placed"):
            r["package"] = "not_placed"
            r.pop("footprint_hint", None)
        if (r.get("symbol_hint"), r.get("footprint_hint"), r.get("package")) != before:
            changed += 1
    return changed
