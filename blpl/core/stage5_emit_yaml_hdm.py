"""Stage 5: emit YAML HDM consumable by the existing yaml_to_kicad.py compiler.

Inputs:
  - bom.json (components, library hints)
  - nets.json (synthesized from Stage 4)
  - design_artifact.json (connector pin_maps, optional)
  - coverage_report.json (optional — prefers confirmed library matches over hints)
  - project.yaml (hand-authored — project name, stackup, net_classes, boundaries,
    and the test_points policy)

Output: hdm.yaml matching the schema yaml_to_kicad.py already consumes.

Deterministic; no LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import schema, symbol_resolution
from . import stage4_synthesize_nets


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


# --- No-connect pins ---------------------------------------------------------


def is_no_connect(signal: str) -> bool:
    """Whether a pinout row declares its pin deliberately unconnected.

    Stage 4's own predicate — imported, not copied, so a pin Stage 4 refuses
    to put on a net is exactly a pin this stage marks no-connect and the two
    can never drift into disagreement.
    """
    return stage4_synthesize_nets.is_no_connect(signal)


def _no_connect_pins(
    local_id: str, design_artifact: dict, connected: set[tuple[str, str]], refdes: str
) -> list[str]:
    """Physical pins the design declares NC and no net actually claims.

    The second condition is the safety: if some upstream path did put a
    NC-named pin on a net, a no-connect marker on top of a net label is an
    ERC error of our own making. A pin is only marked when it is genuinely
    on nothing.
    """
    pins: set[str] = set()
    for conn in design_artifact.get("connectors", []):
        if conn["local_id"] != local_id:
            continue
        for pin in conn.get("pins", []):
            number = str(pin.get("pin") or "").strip()
            signal = str(pin.get("signal") or "")
            if not number:
                continue
            # Declared open, or a host pin named only by its port that no net
            # picked up (Stage 4 drops those and counts them as unassigned) —
            # either way the pin is on nothing, and the schematic should say
            # so with a flag rather than an open pin ERC has to guess about.
            declared = is_no_connect(signal)
            unassigned = bool(stage4_synthesize_nets._PORT_NAME_RE.match(
                stage4_synthesize_nets._normalize_signal(signal)))
            if not (declared or unassigned):
                continue
            if (refdes, number) in connected or (local_id, number) in connected:
                continue
            pins.add(number)
    return sorted(pins, key=lambda v: (len(v), v))


# --- Test points -------------------------------------------------------------

TEST_POINT_POLICIES = ("none", "power", "all")
DEFAULT_TEST_POINT_POLICY = "power"
DEFAULT_TEST_POINT_SYMBOL = "Connector:TestPoint"
DEFAULT_TEST_POINT_FOOTPRINT = "TestPoint:TestPoint_Pad_D1.0mm"


class TestPointConfigError(ValueError):
    """project.yaml asks for test points in a way this stage cannot honour."""


def test_point_config(project_config: dict) -> dict:
    """The ``test_points:`` block of project.yaml, defaults filled in.

    ``power`` is the default on purpose. ``all`` on a real board is hundreds of
    pads nobody placed, and ``none`` leaves a board with no way to probe its
    rails during bring-up — which is the one set of test points every board
    wants regardless of how the rest of test is going to be done.
    """
    raw = project_config.get("test_points") or {}
    if not isinstance(raw, dict):
        raise TestPointConfigError("test_points in project.yaml must be a mapping")
    policy = str(raw.get("policy") or DEFAULT_TEST_POINT_POLICY).strip().lower()
    if policy not in TEST_POINT_POLICIES:
        raise TestPointConfigError(
            f"test_points.policy {policy!r} is not one of {', '.join(TEST_POINT_POLICIES)}"
        )
    return {
        "policy": policy,
        "symbol": str(raw.get("symbol") or DEFAULT_TEST_POINT_SYMBOL),
        "footprint": str(raw.get("footprint") or DEFAULT_TEST_POINT_FOOTPRINT),
    }


def _test_point_nets(nets_out: dict, policy: str) -> list[str]:
    if policy == "none":
        return []
    if policy == "power":
        return sorted(n for n, d in nets_out.items() if d.get("class") == "Power_Bulk")
    return sorted(n for n, d in nets_out.items() if d.get("pads"))


def _synthesize_test_points(
    components_out: dict,
    nets_out: dict,
    cfg: dict,
    *,
    project_dir: Path | None,
    stock_symbols_root: Path | None,
    stock_footprints_root: Path | None,
) -> dict:
    """Add one TP per selected net. Returns the ``synthesis.test_points`` record.

    A test point is not a BOM row — it is copper the pipeline adds so the board
    can be probed — so it is marked ``synthesized`` and never counted as a part.
    The symbol and footprint go through the same resolver as real parts: a
    project may override them, and a name that resolves to nothing must stop
    the stage rather than reach the emitter as a dangling lib_id.
    """
    record = {"policy": cfg["policy"], "count": 0, "symbol": cfg["symbol"], "footprint": cfg["footprint"]}
    targets = _test_point_nets(nets_out, cfg["policy"])
    if not targets:
        return record

    symbol, footprint = cfg["symbol"], cfg["footprint"]
    if project_dir is not None and stock_symbols_root is not None:
        res = symbol_resolution.resolve(
            symbol, project_dir=project_dir, stock_root=stock_symbols_root, pin_count=1
        )
        if res.needs_manual_symbol:
            raise TestPointConfigError(
                f"test_points.symbol {symbol!r} {res.reason}; name a symbol that exists "
                "or set test_points.policy: none"
            )
    if project_dir is not None and stock_footprints_root is not None:
        fres = symbol_resolution.resolve_footprint(
            footprint, project_dir=project_dir, stock_root=stock_footprints_root, pin_count=1
        )
        if fres.needs_manual_symbol:
            raise TestPointConfigError(
                f"test_points.footprint {footprint!r} {fres.reason}; name a footprint that "
                "exists or set test_points.policy: none"
            )

    taken = {r.upper() for r in components_out}
    n = 0
    for net_name in targets:
        # Skip refdes the design already uses — a hand-placed TP3 keeps its name.
        n += 1
        while f"TP{n}" in taken:
            n += 1
        refdes = f"TP{n}"
        taken.add(refdes)
        components_out[refdes] = {
            "value": f"TP_{net_name}",
            "lib_symbol": symbol,
            "footprint": footprint,
            "pin_map": {"1": "1"},
            "synthesized": "test_point",
            "net": net_name,
        }
        nets_out[net_name]["pads"].append([refdes, "1"])
        record["count"] += 1
    return record


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
    board: str | None = None,
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

    # The doc's explicit pins, applied at read time — the same overlay Stage 2
    # ran its coverage under. bom.json is Stage 1's output and Stage 1 is the
    # LLM stage, so without this a Symbol cell edited in the design doc
    # changed nothing here until a paid rerun regenerated the file.
    from . import explicit_pins

    explicit_pins.apply(bom["rows"], project_dir, board)

    cov_by_id = _index_coverage(coverage)
    resolutions: dict[str, symbol_resolution.Resolution] = {}
    footprint_resolutions: dict[str, symbol_resolution.Resolution] = {}

    board_dim = tuple(project_config.get("project", {}).get("dimensions", [100, 80]))
    tp_cfg = test_point_config(project_config)
    # Every (refdes, pin) some net claims — the guard that keeps a no-connect
    # marker off a pin that is, in fact, connected.
    connected: set[tuple[str, str]] = {
        (str(m["refdes"]), str(m["pin"])) for net in nets["nets"] for m in net["members"]
    }
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
        nc = _no_connect_pins(row["local_id"], design_artifact, connected, refdes)
        if nc:
            # What the design says is deliberately open, so the emitter can mark
            # it and ERC stops reporting every declared-NC pin as a mistake.
            comp["no_connect_pins"] = nc
        components_out[refdes] = comp

    nets_out: dict[str, Any] = {}
    for net in nets["nets"]:
        nets_out[net["name"]] = {
            "class": net["class"],
            "pads": [[m["refdes"], m["pin"]] for m in net["members"]],
        }

    tp_record = _synthesize_test_points(
        components_out,
        nets_out,
        tp_cfg,
        project_dir=project_dir,
        stock_symbols_root=stock_symbols_root,
        stock_footprints_root=stock_footprints_root,
    )

    hdm: dict[str, Any] = {}
    for key in ("project", "net_classes", "boundaries"):
        if key in project_config:
            hdm[key] = project_config[key]
    hdm["components"] = components_out
    hdm["nets"] = nets_out
    # Always written, count 0 included: Stage 8 reads this to tell "no test
    # points because the policy said so" from "no test points because the
    # pipeline cannot make them".
    hdm["synthesis"] = {"test_points": tp_record}
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
    board: str | None = None,
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
        board=board,
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
