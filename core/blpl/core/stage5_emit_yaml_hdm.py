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

import re
from pathlib import Path
from typing import Any

import yaml

from . import placement, schema, symbol_resolution
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


class InvalidProjectConfigError(ValueError):
    """project.yaml exists but does not say what it appears to say."""


def _describe(error) -> str:
    where = ".".join(str(p) for p in error.absolute_path) or "(root)"
    return f"{where}: {error.message}"


def ensure_project_config(project_config_path: Path, project_id: str) -> dict:
    """Load project.yaml, validate it, or raise an actionable error.

    **Validated on load, not on write**, and that is the whole point. A form can
    police what it writes; it cannot police a hand-edit, a git merge, a template
    someone copied from another board, or a file this pipeline has never seen
    before. Every one of those reaches Stage 5 the same way, so the check
    belongs where the file is read.

    What it closes: Stage 5 read the board size as
    ``project_config.get("project", {}).get("dimensions") or [100, 80]``. A
    misspelled key, a string where a number belongs, a one-element list — none
    of them errored. They silently produced a 100 x 80 mm board that built,
    routed and reported success at the wrong size. example-handheld's core
    board was 100 x 80 for weeks against a 96.85 x 57.14 target, and nothing in
    the pipeline said so; it surfaced only when a placer started measuring how
    full the board was.

    A file that fails now stops the run and names the field, because a board of
    the wrong size is not a warning.
    """
    if project_config_path.exists():
        with project_config_path.open() as f:
            config = yaml.safe_load(f) or {}
        errors = sorted(
            schema.validator("project_config").iter_errors(config),
            key=lambda e: list(e.absolute_path),
        )
        if errors:
            detail = "\n  ".join(_describe(e) for e in errors[:8])
            more = f"\n  … and {len(errors) - 8} more" if len(errors) > 8 else ""
            raise InvalidProjectConfigError(
                f"{project_config_path} is not a valid project config:\n  {detail}{more}\n"
                f"Nothing was guessed and nothing was defaulted — fix the file and re-run. "
                f"See schemas/project_config.v1.json for what each field accepts."
            )
        return config
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


def _place_components(
    components_out: dict,
    nets_out: dict,
    project_config: dict,
    *,
    project_dir: Path | None,
    stock_footprints_root: Path | None,
) -> dict:
    """Replace the build-time grid with a real placement, and record what happened.

    Returns a record rather than only mutating, because "every part is on the
    board" and "eleven parts did not fit" are both outcomes a later stage needs
    to be able to see. The grid's failure was that it could not express the
    second one: it always succeeded, and the overflow surfaced much later as a
    board nobody could route.

    A placement that fails leaves the grid coordinates alone. They are wrong,
    but they are wrong *visibly* — the alternative is components stacked at the
    origin, which looks like a different bug.
    """
    # No fallback: ensure_project_config has already guaranteed two positive
    # numbers, so a default here could only mask a validator that stopped
    # running.
    dims = project_config["project"]["dimensions"]
    board = (float(dims[0]), float(dims[1]))

    roots: list[Path] = []
    if project_dir is not None and stock_footprints_root is not None:
        roots = [r for r, _ in symbol_resolution.footprint_search_path(
            Path(project_dir), Path(stock_footprints_root))]
    elif stock_footprints_root is not None:
        roots = [Path(stock_footprints_root)]

    hints = (project_config.get("placement") or {}).get("hints") or {}
    result = placement.place(
        components_out, nets_out, board, footprint_roots=roots, hints=hints
    )
    for ref, p in result.placements.items():
        if ref in components_out:
            components_out[ref]["placement"] = p.as_dict()

    record: dict[str, Any] = {
        "placed": len(result.placements),
        "unplaced": sorted(result.unplaced),
        "board_mm": [board[0], board[1]],
        "hinted": sorted(hints),
        "connection_length_mm": round(
            placement.total_connection_length(result.placements, nets_out), 1
        ),
    }
    if result.unplaced:
        record["reasons"] = dict(sorted(result.unplaced.items()))
    if result.notes:
        record["notes"] = result.notes
    return record


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


# --- Net classes -------------------------------------------------------------


class NetClassConfigError(ValueError):
    """project.yaml asks for a net class in a way this stage cannot honour."""


def _project_class_rules(project_config: dict) -> list[tuple[re.Pattern[str], str]]:
    """Optional ``net_class_rules:`` — the project's own pattern → class table.

    Stage 4 guesses a class from the net name using rules that have to work for
    every board, so they cannot know that this project spells its USB class
    ``USB2_HS`` rather than ``USB2_Diff_90Ohm``, or that ``DSI_*`` belongs on
    ``DSI_Diff_100Ohm``. A project that declares a controlled-impedance class
    and never says which nets belong to it gets a class used by nothing and
    differential pairs routed as default single-ended traces — which is exactly
    what happened here to all six MIPI DSI nets.

    Rules are tried in order and the first match wins, ahead of Stage 4's guess.
    """
    raw = project_config.get("net_class_rules") or []
    if not isinstance(raw, list):
        raise NetClassConfigError("net_class_rules in project.yaml must be a list")
    declared = set(project_config.get("net_classes") or {})
    out: list[tuple[re.Pattern[str], str]] = []
    for i, rule in enumerate(raw):
        if not isinstance(rule, dict) or "pattern" not in rule or "class" not in rule:
            raise NetClassConfigError(
                f"net_class_rules[{i}] needs both 'pattern' and 'class'"
            )
        cls = str(rule["class"])
        if cls not in declared:
            raise NetClassConfigError(
                f"net_class_rules[{i}] names class {cls!r}, which is not in net_classes "
                f"({', '.join(sorted(declared)) or 'none declared'})"
            )
        try:
            out.append((re.compile(str(rule["pattern"])), cls))
        except re.error as exc:
            raise NetClassConfigError(
                f"net_class_rules[{i}] pattern {rule['pattern']!r} is not a regex: {exc}"
            ) from exc
    return out


def _reconcile_net_class(
    name: str, guessed: str, rules: list[tuple[re.Pattern[str], str]], declared: set[str]
) -> tuple[str, str | None]:
    """The class this net should carry, and a note if it is not declared.

    Two things go wrong without this. A project rule has to be able to beat
    Stage 4's generic guess. And Stage 4 can assign a class the project never
    declared — ``USB2_Diff_90Ohm`` on a project that declares ``USB2_HS`` — which
    reaches ``.kicad_pro`` as a netclass_pattern naming a class that is not in
    the file, so KiCad falls back to Default without saying so.

    The note is a report, not a correction. Demoting the net here was tried and
    is worse: a project that simply forgot to declare ``Power_Bulk`` would have
    its rails silently rewritten to a signal trace width by the pipeline, which
    is the same silent failure one layer earlier and harder to see. Emit what
    was asked for, and say plainly that the project does not define it.
    """
    for pat, cls in rules:
        if pat.match(name.upper()) or pat.match(name):
            return cls, None
    if declared and guessed not in declared:
        return guessed, (
            f"net {name!r} is classed {guessed!r}, which project.yaml does not declare; "
            f"KiCad will fall back to Default for it. Declare it under net_classes, "
            f"or map the net with net_class_rules."
        )
    return guessed, None


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
    def _patterns(key: str) -> list[re.Pattern[str]]:
        vals = raw.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        if not isinstance(vals, list):
            raise TestPointConfigError(f"test_points.{key} must be a list of patterns")
        out = []
        for v in vals:
            try:
                out.append(re.compile(str(v)))
            except re.error as exc:
                raise TestPointConfigError(
                    f"test_points.{key} pattern {v!r} is not a regex: {exc}"
                ) from exc
        return out

    return {
        "policy": policy,
        "symbol": str(raw.get("symbol") or DEFAULT_TEST_POINT_SYMBOL),
        "footprint": str(raw.get("footprint") or DEFAULT_TEST_POINT_FOOTPRINT),
        # Named nets on top of the policy, and named nets taken back off it.
        # The three policies are a blunt instrument on a real board: `power`
        # leaves every signal net unprobeable, and `all` is not merely excessive
        # but harmful — it puts a stub on differential pairs and on memory buses
        # running at hundreds of MHz, where the test point is an impedance
        # discontinuity rather than a convenience.
        "include": _patterns("include"),
        "exclude": _patterns("exclude"),
        # Off by default, and this is the safety property. A test point on one
        # half of a pair breaks the symmetry the pair exists for. Opting in has
        # to be deliberate.
        "include_diff_pairs": bool(raw.get("include_diff_pairs", False)),
    }


def _test_point_nets(nets_out: dict, cfg: dict) -> list[str]:
    """Which nets get a test point: policy, then include, then exclude.

    Order matters. `include` adds to whatever the policy chose, so a board can
    keep `power` and name the handful of signals bring-up actually needs.
    `exclude` runs last and beats everything, because the nets that must not
    have a stub — differential pairs, a 200 MHz memory bus — have to be
    removable even when a broad policy or a broad include pattern caught them.
    """
    policy = cfg["policy"]
    if policy == "power":
        chosen = {n for n, d in nets_out.items() if d.get("class") == "Power_Bulk"}
    elif policy == "all":
        chosen = {n for n, d in nets_out.items() if d.get("pads")}
    else:
        chosen = set()

    for pat in cfg.get("include") or []:
        chosen |= {n for n, d in nets_out.items() if d.get("pads") and pat.search(n)}

    if not cfg.get("include_diff_pairs", False):
        chosen -= {n for n, d in nets_out.items() if d.get("diff_pair_of")}

    for pat in cfg.get("exclude") or []:
        chosen -= {n for n in chosen if pat.search(n)}

    return sorted(chosen)


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
    if cfg.get("include") or cfg.get("exclude") or cfg.get("include_diff_pairs"):
        record["include"] = [p.pattern for p in cfg.get("include") or []]
        record["exclude"] = [p.pattern for p in cfg.get("exclude") or []]
        record["include_diff_pairs"] = bool(cfg.get("include_diff_pairs", False))
    targets = _test_point_nets(nets_out, cfg)
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

    board_dim = tuple(project_config["project"]["dimensions"])
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

    class_rules = _project_class_rules(project_config)
    declared_classes = set(project_config.get("net_classes") or {})
    class_notes: list[str] = []
    nets_out: dict[str, Any] = {}
    for net in nets["nets"]:
        cls, note = _reconcile_net_class(net["name"], net["class"], class_rules, declared_classes)
        if note:
            class_notes.append(note)
        entry = {
            "class": cls,
            "pads": [[m["refdes"], m["pin"]] for m in net["members"]],
        }
        # Stage 4 works this out and it used to stop here. Carrying it through
        # is what lets the test-point policy leave differential pairs alone
        # without every project having to name them.
        if net.get("diff_pair_of"):
            entry["diff_pair_of"] = net["diff_pair_of"]
        nets_out[net["name"]] = entry

    tp_record = _synthesize_test_points(
        components_out,
        nets_out,
        tp_cfg,
        project_dir=project_dir,
        stock_symbols_root=stock_symbols_root,
        stock_footprints_root=stock_footprints_root,
    )

    # Real placement, over the grid every component was given while it was being
    # built. The grid was never placement — it sized every part the same and ran
    # off the bottom of the board — and a router handed parts that are not on
    # the board can do nothing with them. Done here, after test points, so that
    # synthesized parts are placed too.
    place_record = _place_components(
        components_out, nets_out, project_config,
        project_dir=project_dir, stock_footprints_root=stock_footprints_root,
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
    hdm["synthesis"] = {"test_points": tp_record, "placement": place_record}
    if class_notes:
        hdm["synthesis"]["net_class_notes"] = class_notes
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
