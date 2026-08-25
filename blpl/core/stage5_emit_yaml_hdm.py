"""Stage 5: emit YAML HDM consumable by the existing yaml_to_kicad.py compiler.

Inputs:
  - bom.json (components, library hints)
  - nets.json (synthesized from Stage 4)
  - design_artifact.json (connector pin_maps, optional)
  - coverage_report.json (optional — prefers confirmed library matches over hints)
  - project.yaml (hand-authored — project name, stackup, net_classes, boundaries)

Output: hdm.yaml matching the schema yaml_to_kicad.py already consumes.

Deterministic; no LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import schema, symbol_resolution


_PROJECT_YAML_TEMPLATE = """\
# Hand-authored project config for the HDM pipeline.
# Stage 5 merges this with the generated components/nets sections.

project:
  name: {name}
  board_id: {name}-V1
  dimensions:
    - 100   # width mm
    - 80    # height mm
  stackup:
    layers: 4
    thickness: 1.6
    finish: ENIG

net_classes:
  Default:
    trace_width: 0.2
    clearance: 0.15
    via_dia: 0.6
    via_drill: 0.3
  Power_Bulk:
    trace_width: 0.6
    clearance: 0.2
    via_dia: 0.8
    via_drill: 0.4
  USB3_Diff_90Ohm:
    trace_width: 0.15
    clearance: 0.2
    via_dia: 0.3
    via_drill: 0.15
  PCIe_Diff_85Ohm:
    trace_width: 0.15
    clearance: 0.2
    via_dia: 0.3
    via_drill: 0.15
  DDR4_Diff_90Ohm:
    trace_width: 0.15
    clearance: 0.2
    via_dia: 0.3
    via_drill: 0.15

boundaries:
  board_outline:
    type: rect
    start:
      - 0
      - 0
    end:
      - 100
      - 80
    layer: Edge.Cuts
    width: 0.1
  keepouts: []
  copper_zones: []
"""


class MissingProjectConfigError(RuntimeError):
    """Raised when project.yaml is missing. Message is a user-facing interactive prompt."""


def ensure_project_config(project_config_path: Path, project_id: str) -> dict:
    """Load project.yaml, or raise an actionable error if missing.

    If missing, write a skeleton next to the expected path with `.template` suffix
    so the user can fill it in and rename.
    """
    if project_config_path.exists():
        with project_config_path.open() as f:
            return yaml.safe_load(f) or {}
    template_path = project_config_path.with_suffix(project_config_path.suffix + ".template")
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(_PROJECT_YAML_TEMPLATE.format(name=project_id))
    raise MissingProjectConfigError(
        f"project.yaml is required at {project_config_path}. "
        f"A template was written to {template_path}: edit dimensions/net_classes/boundaries "
        f"as needed, then rename to project.yaml and re-run stage5."
    )


def _best_symbol(row: dict, coverage_row: dict | None) -> str | None:
    """Prefer coverage-confirmed symbol (exact > fuzzy), fall back to bom.symbol_hint."""
    if coverage_row and coverage_row.get("symbol_match"):
        sm = coverage_row["symbol_match"]
        return f"{sm['lib']}:{sm['name']}"
    return row.get("symbol_hint")


def _best_footprint(row: dict, coverage_row: dict | None) -> str | None:
    if coverage_row and coverage_row.get("footprint_match"):
        fm = coverage_row["footprint_match"]
        return f"{fm['lib']}:{fm['name']}"
    return row.get("footprint_hint")


def _default_placement(index: int, total: int, board_dim: tuple[float, float]) -> dict:
    """Arrange components on a simple grid so they're visible in KiCad without overlap."""
    w, h = board_dim
    margin = 5.0
    step = 15.0
    cols = max(1, int((w - 2 * margin) // step))
    col = index % cols
    row = index // cols
    return {
        "x": round(margin + col * step, 3),
        "y": round(margin + row * step, 3),
        "rot": 0.0,
        "side": "top",
    }


def _pin_map_for(
    local_id: str, design_artifact: dict, bom_row: dict | None = None
) -> dict[str, str]:
    """Build ``{logical_signal: physical_pin}`` for a component.

    Resolution order:
      1. Explicit pinout in design_artifact.connectors (hand-authored or Stage 0
         LLM-extracted). This is the source of truth when present — it came
         directly from the user's markdown pinout tables.
      2. ``bom_row["pin_map"]`` — written by Stage 3's classifier for generic
         parts (passives, generic connectors, SOT-23 small-signal).
      3. Empty (no mapping — logical pin used as physical directly).
    """
    for conn in design_artifact.get("connectors", []):
        if conn["local_id"] == local_id:
            return {
                pin["signal"]: str(pin["pin"])
                for pin in conn.get("pins", [])
                if pin.get("signal") and pin.get("pin")
            }
    if bom_row and isinstance(bom_row.get("pin_map"), dict):
        return {str(k): str(v) for k, v in bom_row["pin_map"].items()}
    return {}


def _index_coverage(coverage: dict | None) -> dict[str, dict]:
    if not coverage:
        return {}
    return {r["local_id"]: r for r in coverage.get("rows", [])}


def emit(
    bom: dict,
    nets: dict,
    design_artifact: dict,
    project_config: dict,
    coverage: dict | None = None,
    *,
    project_dir: Path | None = None,
    stock_symbols_root: Path | None = None,
    stock_footprints_root: Path | None = None,
) -> tuple[dict, dict, dict]:
    """Return (HDM dict, symbol resolutions, footprint resolutions).

    Every ``lib_symbol`` is checked against the real libraries before it reaches
    the emitter. Stage 1 hands us LLM-invented symbol names that do not exist, and
    emitting one produces a schematic KiCad cannot open. Anything unresolvable is
    swapped for a pin-count-correct placeholder and flagged — see
    ``symbol_resolution``.
    """
    schema.validate("bom", bom)
    schema.validate("nets", nets)
    schema.validate("design_artifact", design_artifact)

    cov_by_id = _index_coverage(coverage)
    resolutions: dict[str, symbol_resolution.Resolution] = {}
    footprint_resolutions: dict[str, symbol_resolution.Resolution] = {}

    board_dim = tuple(project_config.get("project", {}).get("dimensions", [100, 80]))
    components_out: dict[str, Any] = {}
    rows = bom["rows"]
    not_placed: list[str] = []
    for i, row in enumerate(rows):
        refdes = row.get("refdes") or row["local_id"]
        coverage_row = cov_by_id.get(row["local_id"])
        if symbol_resolution.is_not_placed(
            row.get("footprint") or row.get("footprint_hint") or row.get("package") or ""
        ):
            # Declared as never landing on the board — a bare coin cell in a
            # retainer clip, a wire-terminated speaker whose connector has its
            # own row. Emitting it would put copper under a part that must have
            # none; resolve_footprint would substitute a placeholder header,
            # which is exactly the wrong-copper failure this stage exists to
            # prevent. The row stays in bom.json, so ordering still sees it —
            # the part is bought, just never soldered.
            not_placed.append(refdes)
            continue
        comp: dict[str, Any] = {
            "value": row["mpn"],
            "placement": _default_placement(i, len(rows), board_dim),
        }
        # Sourcing fields. bom.json has carried these all along and the HDM threw
        # them away, so every emitted board had an MPN coverage of zero and could
        # not be quoted, ordered, or fabbed. `value` is not an MPN — it is what
        # gets printed on the silkscreen.
        for hdm_key, row_key in (
            ("mpn", "mpn"),
            ("manufacturer", "manufacturer"),
            ("datasheet", "datasheet_url"),
            ("description", "description"),
        ):
            if row.get(row_key):
                comp[hdm_key] = str(row[row_key])
        fp = _best_footprint(row, coverage_row)
        sym = _best_symbol(row, coverage_row)

        if project_dir is not None and stock_symbols_root is not None:
            res = symbol_resolution.resolve(
                sym,
                project_dir=project_dir,
                stock_root=stock_symbols_root,
                pin_count=row.get("pin_count"),
            )
            resolutions[refdes] = res
            comp["lib_symbol"] = res.ref
            comp["symbol_source"] = res.source
            if res.needs_manual_symbol:
                # Carried into the HDM so every downstream consumer — the emitter,
                # Stage 8, anyone reading hdm.yaml — can see this is not a real part.
                comp["needs_manual_symbol"] = True
                comp["requested_symbol"] = res.requested
        elif sym:
            comp["lib_symbol"] = sym

        if project_dir is not None and stock_footprints_root is not None:
            fres = symbol_resolution.resolve_footprint(
                fp,
                project_dir=project_dir,
                stock_root=stock_footprints_root,
                pin_count=row.get("pin_count"),
            )
            footprint_resolutions[refdes] = fres
            comp["footprint"] = fres.ref
            comp["footprint_source"] = fres.source
            if fres.needs_manual_symbol:
                comp["needs_manual_footprint"] = True
                comp["requested_footprint"] = fres.requested
        elif fp:
            comp["footprint"] = fp
        if row.get("role"):
            comp["role"] = row["role"]
        pin_map = _pin_map_for(row["local_id"], design_artifact, bom_row=row)
        if pin_map:
            comp["pin_map"] = pin_map
        components_out[refdes] = comp

    nets_out: dict[str, Any] = {}
    for net in nets["nets"]:
        nets_out[net["name"]] = {
            "class": net["class"],
            "pads": [[m["refdes"], m["pin"]] for m in net["members"]],
        }

    hdm: dict[str, Any] = {}
    for key in ("project", "net_classes", "boundaries"):
        if key in project_config:
            hdm[key] = project_config[key]
    hdm["components"] = components_out
    hdm["nets"] = nets_out
    if not_placed:
        # Recorded rather than dropped: anyone diffing bom.json against the HDM
        # would otherwise find rows that vanished with nothing saying why.
        hdm["not_placed"] = sorted(not_placed)
    return hdm, resolutions, footprint_resolutions


def run(
    bom_path: Path,
    nets_path: Path,
    design_artifact_path: Path,
    project_config_path: Path,
    output_path: Path,
    coverage_path: Path | None = None,
    *,
    project_dir: Path | None = None,
    stock_symbols_root: Path | None = None,
    stock_footprints_root: Path | None = None,
) -> dict:
    """Emit hdm.yaml. Raises MissingProjectConfigError if project.yaml is not present."""
    bom = schema.load_json(bom_path)
    nets = schema.load_json(nets_path)
    design_artifact = schema.load_json(design_artifact_path)
    project_config = ensure_project_config(project_config_path, design_artifact["project_id"])
    coverage = schema.load_json(coverage_path) if coverage_path and coverage_path.exists() else None
    hdm, resolutions, footprint_resolutions = emit(
        bom,
        nets,
        design_artifact,
        project_config,
        coverage,
        project_dir=project_dir,
        stock_symbols_root=stock_symbols_root,
        stock_footprints_root=stock_footprints_root,
    )

    # The "you must draw these yourself" report. Written every run — including when
    # it is empty — so its absence never reads as "nothing to worry about".
    if project_dir is not None:
        (output_path.parent / "manual_library_work.md").write_text(
            symbol_resolution.render_manual_symbols_md(
                resolutions, project_dir, footprint_resolutions
            ),
            encoding="utf-8",
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        yaml.safe_dump(hdm, f, sort_keys=False)
    return hdm
