"""Reusing an extracted module in a new design.

Extraction (``blpl.importer_kicad.modules``) is only half the payoff. The other
half is this: a Stage 0 design document naming a module by name, and getting its
parts without retyping them.

The markdown side is deliberately plain — a ``## Modules`` heading and a bullet
per module::

    ## Modules

    - ltc4015-charger — the battery charger from dev.03, unchanged
    - lora-frontend

A carrier board is mostly this. Writing it as prose bullets rather than a table
keeps it readable in the same document a human is already using to think, which
is the whole reason the pipeline starts from markdown.

Expansion adds each module's parts to the artifact with the module name
prefixed onto the refdes (``ltc4015-charger.U1``), because two modules on one
carrier will both have a U1 and silently merging them would be a wiring error
nobody would see until the board came back.

Nothing is dropped silently: a module that cannot be found, or whose BOM is
unreadable, becomes a warning naming the module and where it was looked for.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..importer_kicad.modules import MODULES_DIRNAME

# "## Modules" / "### Modules used" — the heading, then bullets until the next
# heading. Case-insensitive because nobody capitalises consistently.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+modules\b.*$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")


def parse_modules_section(text: str) -> list[str]:
    """Module names listed under a ``Modules`` heading, in document order."""
    names: list[str] = []
    in_section = False
    for line in text.splitlines():
        if _HEADING.match(line):
            in_section = True
            continue
        if in_section and re.match(r"^\s{0,3}#{1,6}\s+", line):
            break
        if not in_section:
            continue
        m = _BULLET.match(line)
        if not m:
            continue
        # "name — why we chose it": the name is everything before the first dash
        # used as a separator. A hyphen inside a name is common
        # (ltc4015-charger), so only a spaced dash separates.
        name = re.split(r"\s+[—–-]\s+", m.group(1), maxsplit=1)[0].strip()
        name = name.strip("`*_")
        if name and name not in names:
            names.append(name)
    return names


def module_roots(project_dir: Path) -> list[Path]:
    """Where to look for a named module, highest priority first."""
    from .symbol_resolution import shared_modules_root

    return [Path(project_dir) / MODULES_DIRNAME, shared_modules_root()]


def find_module(name: str, project_dir: Path) -> Path | None:
    for root in module_roots(project_dir):
        candidate = root / name
        if (candidate / "module.yaml").is_file():
            return candidate
    return None


def expand(artifact: dict, project_dir: Path, names: list[str]) -> None:
    """Fold the named modules' parts into a Stage 0 artifact, in place.

    Called after table extraction so a design can both name a module and add
    discrete parts around it — which is what a carrier board actually is.
    """
    if not names:
        return

    components: list[dict] = artifact.setdefault("components", [])
    existing = {c.get("local_id") for c in components}
    warnings: list[dict] = artifact.setdefault("warnings", [])
    used: list[str] = []

    for name in names:
        module_dir = find_module(name, project_dir)
        if module_dir is None:
            warnings.append(
                {
                    "code": "STAGE0-006",
                    "summary": f"module {name!r} is named in the design but was not found.",
                    "fix": (
                        "Extract it from an existing board, or copy its directory into "
                        + ", ".join(str(r) for r in module_roots(project_dir))
                        + "."
                    ),
                }
            )
            continue

        bom_path = module_dir / "bom.json"
        try:
            rows = json.loads(bom_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warnings.append(
                {
                    "code": "STAGE0-007",
                    "summary": f"module {name!r} has no readable BOM ({exc}).",
                    "fix": (
                        "Re-extract the module; a module without a BOM contributes no parts "
                        "and the board would be built missing that whole block."
                    ),
                }
            )
            continue

        added = 0
        for row in rows if isinstance(rows, list) else []:
            refdes = str(row.get("refdes", "")).strip()
            if not refdes:
                continue
            local_id = f"{name}.{refdes}"
            if local_id in existing:
                continue
            entry = {
                "local_id": local_id,
                "description": str(row.get("value") or refdes),
                "source_ref": {"file": str(module_dir / "bom.json")},
            }
            if row.get("mpn"):
                entry["part_hint"] = str(row["mpn"])
            if row.get("footprint"):
                entry["package_hint"] = str(row["footprint"])
            components.append(entry)
            existing.add(local_id)
            added += 1

        used.append(name)
        if not added:
            warnings.append(
                {
                    "code": "STAGE0-008",
                    "summary": f"module {name!r} was found but contributed no parts.",
                    "fix": "Check its bom.json — an empty module is almost always a bad extraction.",
                }
            )

    if used:
        artifact["modules"] = used
    if not warnings:
        artifact.pop("warnings", None)
