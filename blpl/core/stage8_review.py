"""Stage 8: design review.

Runs the kicad-happy analyzers over the Stage 6 output and writes a consolidated
review_report.json (plus a human-readable review.md) into .pipeline/.

Stage 7 asks "is this a legal KiCad project?" (KLC/ERC/DRC, via kicad-cli).
Stage 8 asks "is this a good board, and did the pipeline emit what the BOM said?"

The second half of that question is what makes this stage BLPL-specific rather
than a plain kicad-happy wrapper. A finding on an emitted board has one of three
provenances:

  emitter   — the pipeline lost or mangled data it was given. `bom.json` carries
              an MPN for every row but the emitted schematic has none; the placer
              dropped a footprint inside a keepout that project.yaml declared.
              These are BLPL bugs. Fix the emitter, not the board.
  design    — a genuine electrical problem in the design the user authored.
              Fix the markdown (or the design).
  expected  — a consequence of something that did not run *this time*, with
              the reason attached: the autorouter was not available, no
              distributor credentials were configured. Reporting these as
              failures would train the user to ignore the report; reporting
              them without the reason would let them become permanent.

Classifying provenance is the whole point: an unclassified review of a generated
board is ~90% noise, because the generator's own limitations swamp the real
findings. And "expected" is deliberately not a static list any more. Each rule
in it is excused only when the pipeline can show why — an autoroute report
saying the jar was missing, an HDM saying test points were synthesised under a
stated policy — so the moment the tooling is there, the same finding becomes a
design issue with a fix attached instead of quietly staying excused.

Analyzer availability is probed at runtime; a missing kicad-happy checkout
produces a `skipped` result, not a failure — same contract as Stage 7.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from . import schema
from .project_manifest import artifact_path

_PIPELINE_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PIPELINE_ROOT.parent

# Rule IDs whose presence on a *generated* board indicts the emitter rather than
# the design. Each maps to the BLPL component that owns the fix.
_EMITTER_RULES: dict[str, tuple[str, str]] = {
    "RS-001": (
        "emitter/sch.py",
        "Rail has no declared source: the emitter writes no PWR_FLAG and no "
        "power_out pin, so every externally-fed rail reads as undriven.",
    ),
    "KO-001": (
        "emitter/pcb.py::_auto_grid_position",
        "Component placed inside a keepout that project.yaml declares. The grid "
        "placer does not read the keepout list it emits.",
    ),
    "SS-001": (
        "emitter/sch.py",
        "Sourcing blocker from missing MPNs. If bom.json carries MPNs, the "
        "emitter dropped them instead of writing them as symbol properties.",
    ),
    # Schematic and PCB are both emitted from one hdm.yaml. On a hand-drawn
    # project a sch/PCB disagreement means the two drifted apart and is a real
    # design-sync bug; here it is arithmetically impossible unless the emitter
    # wrote the two files inconsistently. So XV-* is always ours.
    "XV-001": (
        "emitter/pcb.py",
        "Component present in the emitted schematic but not the emitted PCB. "
        "Both come from the same hdm.yaml, so the two writers disagree.",
    ),
    "XV-002": (
        "emitter/sch.py + emitter/pcb.py",
        "Same refdes carries a different Value in the schematic than in the PCB. "
        "Both writers read one hdm.yaml row; they are choosing different fields.",
    ),
}

# Rules that are excused only conditionally — each `_classify` branch below
# states the condition and the reason lands in the report next to the count.
# Nothing in here is excused unconditionally any more.
_CONDITIONAL_RULES = ("RS-001", "RT-001", "TE-001", "LC-007", "DS-001")

# The three names kicad-happy's lifecycle audit reads its credentials under.
# The mapping from this deployment's own names lives in agent/kicad_happy.py.
_LIFECYCLE_ENV = ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET",
                  "MOUSER_SEARCH_API_KEY", "ELEMENT14_API_KEY")


class KicadHappyNotFound(RuntimeError):
    """Raised when the review analyzers cannot be located."""


def _find_kicad_happy() -> Path | None:
    """Locate the kicad-happy skills/ directory.

    Order: explicit env override, then the in-tree submodule.
    """
    env = os.environ.get("BLPL_KICAD_HAPPY")
    candidates = [Path(env)] if env else []
    candidates.append(_REPO_ROOT / "kicad-happy")
    for base in candidates:
        if (base / "skills" / "kicad" / "scripts" / "analyze_schematic.py").exists():
            return base
    return None


def _run_analyzer(
    script: Path, args: list[str], out_path: Path, env: dict[str, str] | None = None
) -> dict:
    """Run one analyzer script, capturing its JSON output.

    The analyzers exit non-zero when they find blocking issues, which is a
    successful run producing findings — not a crash. We distinguish the two by
    whether parseable JSON landed on disk, mirroring how kicad-happy's own
    GitHub Action treats them.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(script), *args, "-o", str(out_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
        env=env,
    )
    result: dict = {
        "ok": True,
        "skipped": False,
        "exit_code": proc.returncode,
        "report_json": None,
        "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
    }
    if out_path.exists():
        try:
            json.loads(out_path.read_text())
            result["report_json"] = str(out_path)
            return result
        except json.JSONDecodeError:
            pass
    # No parseable JSON: the analyzer actually failed.
    result["ok"] = False
    result["reason"] = "analyzer produced no parseable JSON"
    return result


def _run_spice(schematic_json: Path, review_dir: Path, *, pcb_json: Path | None) -> dict:
    """Simulate the detected subcircuits, in the analyzer result shape.

    A missing simulator is by far the likeliest outcome — ngspice is a separate
    install — so it lands here as a *skip with a reason*, exactly like a missing
    kicad-happy checkout. The alternative, letting a review silently omit the
    section, would let "nothing failed" and "nothing ran" look identical.
    """
    from ..agent.tools.spice import simulate

    run = simulate(schematic_json, review_dir / "spice.json", pcb_json=pcb_json)
    result: dict = {
        "ok": run.ok,
        "skipped": run.skipped,
        "nothing_to_simulate": run.nothing_to_simulate,
        "nothing_measured": run.nothing_measured,
        "report_json": run.report_json or None,
        "simulator": run.simulator,
        "parasitics": run.parasitics,
        "parasitics_note": run.parasitics_note,
        "counts": run.counts,
        "headline": run.headline(),
    }
    if run.reason:
        result["reason"] = run.reason
    return result


def _load(path: str | None) -> dict:
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Lifecycle audit — runs when it can, says why when it cannot
# ---------------------------------------------------------------------------


def lifecycle_env() -> tuple[dict[str, str] | None, str]:
    """The environment for a lifecycle-enabled analyzer run, or why there is none.

    kicad-happy's audit reads DigiKey / Mouser / element14 credentials under
    its own names; this deployment may hold them under others (the OAuth pair
    especially). The resolver in agent/kicad_happy.py owns that remap, so it is
    asked here rather than reimplemented. LCSC needs no key, but an audit that
    can only reach LCSC is not the audit anyone means by "lifecycle", so at
    least one keyed distributor has to be configured for the auto mode to run.
    """
    try:
        from ..agent.kicad_happy import CredResolver
    except Exception:  # pragma: no cover - the agent package is always present
        return None, "credential resolver unavailable"
    resolver = CredResolver()
    keyed = [d for d in resolver.configured() if d != "lcsc"]
    if not keyed:
        return None, (
            "no distributor credentials configured — set MOUSER_SEARCH_API_KEY, "
            "DIGIKEY_CLIENT_ID + DIGIKEY_CLIENT_SECRET, or ELEMENT14_API_KEY, or pass "
            "--lifecycle to run against LCSC alone"
        )
    env = dict(os.environ)
    for wanted in _LIFECYCLE_ENV:
        val = resolver.value(wanted)
        if val:
            env[wanted] = val
    return env, ", ".join(keyed)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class _Context:
    """Everything `_classify` needs to know about what the pipeline did this run.

    Kept together so the excuse for each conditional rule is computed once and
    reported once, next to the count, rather than restated per finding.
    """

    def __init__(
        self,
        *,
        pwr_flag_nets: set[str],
        autoroute: dict | None,
        test_points: dict | None,
        lifecycle_reason: str | None,
        emitter_evidence: dict,
    ) -> None:
        self.pwr_flag_nets = pwr_flag_nets
        self.autoroute = autoroute or {}
        self.test_points = test_points or {}
        self.lifecycle_reason = lifecycle_reason
        self.emitter_evidence = emitter_evidence
        # rule_id -> the sentence that explains why it is excused this run.
        self.reasons: dict[str, str] = {}

    @property
    def routed(self) -> bool:
        return bool(self.autoroute.get("attempted") and self.autoroute.get("ok"))

    def excuse(self, rule: str, why: str) -> tuple[str, None]:
        self.reasons.setdefault(rule, why)
        return "expected", None


def _classify(finding: dict, ctx: _Context) -> tuple[str, str | None]:
    """Return (provenance, owner) for one analyzer finding."""
    rule = finding.get("rule_id", "")

    if rule == "RT-001":
        if ctx.routed:
            # A router had a go and left this net open. That is a real
            # statement about the board — congestion, a constraint the router
            # could not meet — and the fix is an interactive route, not a shrug.
            return "design", None
        why = ctx.autoroute.get("reason") or "autorouting was not attempted"
        return ctx.excuse(
            "RT-001",
            f"nets are unrouted because {why}. Install Freerouting and re-run "
            "`blpl autoroute` (or `blpl run`) and these become real findings.",
        )

    if rule == "TE-001":
        # Test points are synthesised under a policy the project chose. Under
        # `all` this finding is coverage arithmetic on nets that already have
        # one; under `power`/`none` it is the designer's call and stays a
        # design issue with the knob named in the recommendation.
        return "design", None

    if rule == "LC-007":
        return ctx.excuse(
            "LC-007",
            f"lifecycle audit did not run: {ctx.lifecycle_reason or 'skipped'}.",
        )

    if rule == "DS-001":
        return ctx.excuse("DS-001", "datasheet corpus is not synced by default.")

    if rule in _EMITTER_RULES:
        owner, _ = _EMITTER_RULES[rule]
        # SS-001 only indicts the emitter if the BOM actually had MPNs to lose.
        if rule == "SS-001" and not ctx.emitter_evidence.get("mpn_dropped"):
            return "design", None
        # RS-001 on a rail we DID flag: kicad-happy versions before the
        # fix/pwr-flag-visible-to-rail-audit change built their net map with
        # PWR_FLAG pins excluded and then looked for one in it, so they reported
        # every flagged rail as unsourced. Current kicad-happy sees the flag and
        # never emits this; the excuse is kept for an older checkout and says so.
        if rule == "RS-001" and ctx.pwr_flag_nets:
            if set(finding.get("nets") or []) & ctx.pwr_flag_nets:
                return ctx.excuse(
                    "RS-001",
                    "reported on a rail BLPL put a PWR_FLAG on — this kicad-happy "
                    "checkout predates the fix that lets its rail audit see the flag "
                    "(git submodule update kicad-happy).",
                )
        return "emitter", owner
    return "design", None


# ---------------------------------------------------------------------------
# Cross-checks the analyzers cannot do
# ---------------------------------------------------------------------------


def _stage1_crosscheck(design_artifact: dict, bom: dict) -> list[dict]:
    """Did Stage 1 keep every component Stage 0 found?

    Stage 0 is deterministic — if it read 46 components out of the markdown, then
    46 components are what the user wrote. Stage 1 resolves those through an LLM,
    and an LLM that returns a short list produces a quietly smaller board rather
    than an error. That failure is invisible downstream: the emitter faithfully
    emits whatever survived, so the board looks *correct*, just missing a third of
    the design. Compare the two counts and say so.
    """
    checks: list[dict] = []
    da_ids = {c.get("local_id") for c in design_artifact.get("components", []) if c.get("local_id")}
    bom_ids = {r.get("local_id") for r in bom.get("rows", []) if r.get("local_id")}
    if not da_ids:
        return checks

    lost = sorted(da_ids - bom_ids)
    if lost:
        shown = ", ".join(lost[:12]) + (f", … (+{len(lost) - 12} more)" if len(lost) > 12 else "")
        checks.append(
            {
                "check": "stage1_component_loss",
                "severity": "error",
                "owner": "core/stage1_resolve_bom.py",
                "summary": (
                    f"Stage 0 parsed {len(da_ids)} components from the markdown but only "
                    f"{len(bom_ids)} reached the BOM — {len(lost)} were dropped: {shown}"
                ),
                "recommendation": (
                    "Stage 1's LLM resolution returned fewer rows than it was given and the "
                    "pipeline accepted it silently. Stage 1 should assert that every input "
                    "local_id appears in its output, and fail loudly (or fall back to a "
                    "deterministic row) rather than shipping a board missing a third of its "
                    "components."
                ),
            }
        )
    return checks


def _shown(names: list[str], limit: int = 12) -> str:
    return ", ".join(names[:limit]) + (f", … (+{len(names) - limit} more)" if len(names) > limit else "")


def placeable_refdes(
    bom: dict, hdm: dict, project_dir: Path | None = None, board: str | None = None
) -> set[str]:
    """The BOM rows that are supposed to land on the board.

    Three things legitimately keep a row off the copper and none of them is a
    leak: a ``not_placed`` package (a bare coin cell that sits in a retainer
    with its own row), the same flag stated only in the design doc and applied
    as an overlay when the stages read bom.json, and Stage 5's own record of
    what it left out. Counting those as "missing symbols" is how a correct
    board failed its own review.
    """
    from . import explicit_pins, symbol_resolution

    overlay = explicit_pins.load(project_dir, board) if project_dir is not None else {}
    skipped = set(hdm.get("not_placed") or [])
    out: set[str] = set()
    for r in bom.get("rows", []):
        lid = r.get("local_id")
        if not lid or lid in skipped:
            continue
        if symbol_resolution.is_not_placed(r.get("package")):
            continue
        if overlay.get(lid, {}).get("not_placed"):
            continue
        out.add(lid)
    return out


def synthesized_refdes(hdm: dict) -> set[str]:
    """Parts Stage 5 added that no BOM row asked for — test points today."""
    return {
        ref for ref, comp in (hdm.get("components") or {}).items()
        if isinstance(comp, dict) and comp.get("synthesized")
    }


def _emitter_crosschecks(
    bom: dict, sch: dict, pcb: dict, hdm: dict | None = None, project_dir: Path | None = None,
    board: str | None = None,
) -> list[dict]:
    """Compare what the BOM promised against what the emitter actually wrote.

    These checks are invisible to kicad-happy: it only ever sees the emitted
    files, so it cannot know a part was supposed to be there and isn't.

    By name, not by count, wherever the analyzer JSON carries names. A count
    says "one is missing"; a name says which one, and on the board that
    motivated this the answer was a coin cell that was never meant to be
    placed — a count-based check reported a leak on a correct board, and would
    have kept doing so on every board with a not_placed row.
    """
    checks: list[dict] = []
    rows = bom.get("rows", [])
    if not rows:
        return checks
    hdm = hdm or {}

    expected = placeable_refdes(bom, hdm, project_dir, board)
    synthesized = synthesized_refdes(hdm)
    bom_count = len(expected)
    bom_with_mpn = sum(1 for r in rows if r.get("mpn") and r.get("local_id") in expected)

    sch_stats = sch.get("statistics", {})
    sch_count = sch_stats.get("total_components", 0)
    sch_with_mpn = sch.get("bom_lock", {}).get("components_with_mpn", 0)

    if bom_with_mpn and sch_count and not sch_with_mpn:
        checks.append(
            {
                "check": "mpn_dropped",
                "severity": "error",
                "owner": "emitter/sch.py",
                "summary": (
                    f"bom.json carries an MPN on {bom_with_mpn}/{bom_count} rows, but the "
                    f"emitted schematic has 0 symbols with an MPN property."
                ),
                "recommendation": (
                    "Write MPN and Manufacturer as (property ...) fields on each symbol in "
                    "emitter/sch.py. Without them the board can never be sourced or fabbed."
                ),
            }
        )

    # Guard on whether the artifact was emitted/analyzed at all (a non-empty
    # analysis dict), NOT on the emitted count. A count of 0 — the *worst*
    # leakage, every symbol/footprint vanished — must still fire, but a
    # genuinely absent artifact (no .kicad_pcb supplied → `pcb` is `{}`) must
    # be skipped, or we'd flag "0 footprints" on a run that never produced a
    # PCB. `sch`/`pcb` truthiness draws exactly that line.
    sch_names = {
        str(c.get("reference"))
        for c in sch.get("components", []) or []
        if isinstance(c, dict) and c.get("reference") and not str(c["reference"]).startswith("#")
    }
    pcb_names = {
        str(f.get("reference"))
        for f in pcb.get("footprints", []) or []
        if isinstance(f, dict) and f.get("reference")
    }

    if sch:
        if sch_names or "components" in sch:
            missing = sorted(expected - sch_names)
            if missing:
                checks.append(
                    {
                        "check": "symbol_leakage",
                        "severity": "error",
                        "owner": "emitter/sch.py",
                        "components": missing,
                        "summary": (
                            f"{len(missing)} of {bom_count} placeable BOM row(s) have no symbol "
                            f"in the emitted schematic: {_shown(missing)}"
                        ),
                        "recommendation": (
                            "Each of these rows reached hdm.yaml (or should have) and the "
                            "schematic writer dropped it — usually an unresolvable lib_symbol "
                            "it skipped instead of substituting a placeholder for."
                        ),
                    }
                )
        elif sch_count < bom_count:
            checks.append(
                {
                    "check": "symbol_leakage",
                    "severity": "error",
                    "owner": "emitter/sch.py",
                    "summary": f"bom.json has {bom_count} placeable rows but only {sch_count} symbols were emitted.",
                    "recommendation": "Components are being dropped between Stage 5 and Stage 6.",
                }
            )

    if pcb:
        if pcb_names or "footprints" in pcb:
            missing = sorted(expected - pcb_names)
            if missing:
                checks.append(
                    {
                        "check": "footprint_leakage",
                        "severity": "error",
                        "owner": "emitter/pcb.py",
                        "components": missing,
                        "summary": (
                            f"{len(missing)} of {bom_count} placeable BOM row(s) have no footprint "
                            f"in the emitted PCB: {_shown(missing)}"
                        ),
                        "recommendation": (
                            "Either the footprint was never placed or it was placed under the "
                            "wrong reference (a library footprint whose Reference field the "
                            "writer did not overwrite)."
                        ),
                    }
                )
            unexplained = sorted(pcb_names - expected - synthesized - sch_names)
            if unexplained:
                checks.append(
                    {
                        "check": "footprint_unaccounted",
                        "severity": "error",
                        "owner": "emitter/pcb.py",
                        "components": unexplained,
                        "summary": (
                            f"{len(unexplained)} footprint(s) in the emitted PCB match no BOM row "
                            f"and nothing Stage 5 synthesised: {_shown(unexplained)}"
                        ),
                        "recommendation": (
                            "A footprint under a reference the pipeline never assigned — "
                            "typically a library part whose default Reference survived."
                        ),
                    }
                )
        elif pcb_count_lt(pcb, bom_count):
            checks.append(
                {
                    "check": "footprint_leakage",
                    "severity": "error",
                    "owner": "emitter/pcb.py",
                    "summary": (
                        f"bom.json has {bom_count} placeable rows but only "
                        f"{len(pcb.get('footprints', []))} footprints were placed."
                    ),
                    "recommendation": (
                        "Rows without a resolved footprint_hint are silently skipped. They should "
                        "surface as a Stage 2/3 coverage miss instead of vanishing here."
                    ),
                }
            )

    return checks


def pcb_count_lt(pcb: dict, bom_count: int) -> bool:
    return len(pcb.get("footprints", []) or []) < bom_count


def _placeholder_check(hdm: dict) -> list[dict]:
    """Components whose symbol or footprint is a stand-in, not the real part.

    Stage 5 substitutes these so the board still opens and renders — which is
    exactly what makes them dangerous. This is the last gate before someone
    treats the output as fabricable, so it is an error, not a warning.
    """
    out: list[dict] = []
    for refdes, comp in sorted((hdm.get("components") or {}).items()):
        if comp.get("needs_manual_footprint"):
            out.append(
                {
                    "check": "placeholder_footprint",
                    "severity": "error",
                    "refdes": refdes,
                    "summary": (
                        f"{refdes}: no real footprint exists for "
                        f"{comp.get('requested_footprint', '(none)')!r} — a 2.54mm header "
                        f"is standing in for it. This land pattern is wrong copper."
                    ),
                }
            )
        if comp.get("needs_manual_symbol"):
            out.append(
                {
                    "check": "placeholder_symbol",
                    "severity": "error",
                    "refdes": refdes,
                    "summary": (
                        f"{refdes}: no real symbol exists for "
                        f"{comp.get('requested_symbol', '(none)')!r} — a generic connector "
                        f"is standing in for it."
                    ),
                }
            )
    return out


def _test_point_recommendation(finding: dict, test_points: dict) -> str | None:
    """Replace kicad-happy's "add test points" with the knob that adds them."""
    if finding.get("rule_id") != "TE-001":
        return finding.get("recommendation")
    policy = test_points.get("policy") or "power"
    count = test_points.get("count", 0)
    if policy == "all":
        return (
            f"Stage 5 synthesised {count} test points under test_points.policy: all; the "
            "remaining uncovered nets have no pads to probe. Nothing to do."
        )
    return (
        f"Stage 5 synthesised {count} test point(s) under test_points.policy: {policy}. "
        "Set `test_points: {policy: all}` in project.yaml to put one on every net, or "
        "add TP rows to the design for the nets that matter."
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_markdown(report: dict) -> str:
    lines = ["# Stage 8 — Design Review", ""]
    if report.get("board"):
        lines += [f"Board: **{report['board']}**", ""]
    counts = report["summary"]
    lines.append(
        f"**{counts['emitter']} emitter defects** · "
        f"**{counts['design']} design issues** · "
        f"{counts['expected']} expected (not run this time — reasons below)"
    )
    lines.append("")

    if report.get("placeholders"):
        ph = report["placeholders"]
        fps = [p for p in ph if p["check"] == "placeholder_footprint"]
        syms = [p for p in ph if p["check"] == "placeholder_symbol"]
        lines += [
            "## DO NOT FABRICATE THIS BOARD",
            "",
            f"{len(fps)} component(s) carry a **placeholder footprint** and "
            f"{len(syms)} carry a **placeholder symbol**. Stage 5 substituted generic "
            "stand-ins because the real library parts do not exist, so the board opens, "
            "routes, and renders while being wrong.",
            "",
        ]
        for p in ph:
            lines.append(f"- **{p['refdes']}** — {p['summary']}")
        lines += [
            "",
            "Draw the real parts into the project's `libraries/` directory and re-run stage5.",
            "See `.pipeline/manual_library_work.md`.",
            "",
        ]

    if report["emitter_defects"]:
        lines += [
            "## Emitter defects — fix the pipeline, not the board",
            "",
            "The generated files disagree with the artifacts they were generated from.",
            "",
        ]
        for d in report["emitter_defects"]:
            owner = f" _(owner: `{d['owner']}`)_" if d.get("owner") else ""
            lines.append(f"- **{d.get('rule_id') or d.get('check')}** — {d['summary']}{owner}")
            if d.get("recommendation"):
                lines.append(f"  - {d['recommendation']}")
        lines.append("")

    if report["design_issues"]:
        lines += ["## Design issues — fix the design markdown", ""]
        for d in report["design_issues"]:
            lines.append(f"- **[{d['severity'].upper()}] {d.get('rule_id','')}** — {d['summary']}")
            if d.get("rule_id") in ("TE-001", "RT-001") and d.get("recommendation"):
                lines.append(f"  - {d['recommendation']}")
        lines.append("")

    routing = report.get("autoroute") or {}
    if routing:
        lines += ["## Routing", ""]
        if routing.get("attempted") and routing.get("ok"):
            left = routing.get("unrouted")
            lines.append(
                f"Freerouting ran ({routing.get('passes', '?')} passes)"
                + (f"; {left} net(s) left unrouted." if left is not None else ".")
            )
        else:
            lines.append(f"Not routed: {routing.get('reason', 'autorouting was not attempted')}.")
        lines.append("")

    sim = report.get("simulation") or {}
    if sim.get("skipped"):
        # Said out loud rather than omitted: a review with no simulation section
        # reads as "the analog side is fine", which is the one thing it does not
        # mean.
        lines += [
            "## Simulation — not run",
            "",
            f"No subcircuit was simulated: {sim.get('reason', 'unknown reason')}.",
            "Filter cutoffs, divider ratios and opamp gains in this design are "
            "unverified by simulation.",
            "",
        ]
    elif sim.get("nothing_to_simulate"):
        lines += [
            "## Simulation — nothing to simulate",
            "",
            f"{sim.get('simulator', 'SPICE')} ran and found no subcircuit it could build a "
            "testbench for. This is **not** a pass: filters, dividers and crystal load "
            "networks are only simulatable once their passives are in the schematic, so a "
            "crystal drawn without its load caps is skipped rather than failed.",
            "",
        ]
    elif sim.get("nothing_measured"):
        lines += [
            "## Simulation — no measurements came back",
            "",
            sim.get("headline") or "Every testbench ran and returned nothing.",
            "",
        ]
    elif sim.get("counts"):
        c = sim["counts"]
        lines += [
            "## Simulation",
            "",
            f"{c.get('total', 0)} subcircuits simulated with {sim.get('simulator', 'SPICE')} "
            f"({sim.get('parasitics_note') or 'ideal nets'}) — "
            f"{c.get('pass', 0)} pass, {c.get('warn', 0)} warn, {c.get('fail', 0)} fail, "
            f"{c.get('skip', 0)} skip.",
            "",
        ]

    if report["expected"]:
        lines += ["## Expected — did not run this time", ""]
        reasons = report.get("expected_reasons") or {}
        for d in report["expected"]:
            why = reasons.get(d.get("rule_id", ""), "")
            lines.append(f"- {d.get('rule_id','')} ×{d['count']} — {why}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    project_dir: Path,
    *,
    sch_path: Path | None = None,
    pcb_path: Path | None = None,
    emc: bool = True,
    spice: bool = True,
    board: str | None = None,
    lifecycle: bool | None = None,
) -> dict:
    """Run Stage 8 and write review_report.json + review.md into .pipeline/.

    ``board`` qualifies every artifact read and written, the way the other
    stages do. ``lifecycle`` is None for "run the distributor lifecycle audit
    if credentials are configured", True to force it (LCSC needs no key), False
    to skip it.
    """
    project_dir = Path(project_dir)
    pipeline_dir = project_dir / ".pipeline"
    review_dir = pipeline_dir / ("review" if board is None else f"review.{board}")
    review_dir.mkdir(parents=True, exist_ok=True)

    def _p(name: str, suffix: str = "json") -> Path:
        return artifact_path(project_dir, name, board=board, suffix=suffix)

    base = _find_kicad_happy()
    if base is None:
        report = {
            "ok": True,
            "skipped": True,
            "board": board,
            "reason": (
                "kicad-happy not found. Expected at <repo>/kicad-happy "
                "(git submodule update --init) or set BLPL_KICAD_HAPPY."
            ),
        }
        _p("review_report").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return report

    kicad_scripts = base / "skills" / "kicad" / "scripts"
    emc_scripts = base / "skills" / "emc" / "scripts"

    analyzers: dict[str, dict] = {}

    # Lifecycle: on when asked, on when it can be, and the reason it is off
    # travels into the report rather than into a static "known limitation".
    lifecycle_args: list[str] = []
    lifecycle_reason: str | None = None
    analyzer_env: dict[str, str] | None = None
    if lifecycle is False:
        lifecycle_reason = "skipped on request (--no-lifecycle)"
    else:
        env, why = lifecycle_env()
        if env is not None or lifecycle is True:
            lifecycle_args = ["--lifecycle"]
            analyzer_env = env
        else:
            lifecycle_reason = why

    if sch_path is not None and sch_path.exists():
        analyzers["schematic"] = _run_analyzer(
            kicad_scripts / "analyze_schematic.py",
            [str(sch_path), *lifecycle_args],
            review_dir / "schematic.json",
            env=analyzer_env,
        )
    else:
        analyzers["schematic"] = {"ok": True, "skipped": True, "reason": "no .kicad_sch supplied"}

    if pcb_path is not None and pcb_path.exists():
        analyzers["pcb"] = _run_analyzer(
            kicad_scripts / "analyze_pcb.py", [str(pcb_path), "--full"], review_dir / "pcb.json"
        )
    else:
        analyzers["pcb"] = {"ok": True, "skipped": True, "reason": "no .kicad_pcb supplied"}

    sch_json = _load(analyzers["schematic"].get("report_json"))
    pcb_json = _load(analyzers["pcb"].get("report_json"))

    # Cross-analysis and EMC are second-pass consumers: they read the analyzer
    # JSON, not the KiCad files, so they need both halves to have succeeded.
    if sch_json and pcb_json:
        analyzers["cross"] = _run_analyzer(
            kicad_scripts / "cross_analysis.py",
            [
                "--schematic",
                analyzers["schematic"]["report_json"],
                "--pcb",
                analyzers["pcb"]["report_json"],
            ],
            review_dir / "cross.json",
        )
        if emc:
            analyzers["emc"] = _run_analyzer(
                emc_scripts / "analyze_emc.py",
                [
                    "--schematic",
                    analyzers["schematic"]["report_json"],
                    "--pcb",
                    analyzers["pcb"]["report_json"],
                ],
                review_dir / "emc.json",
            )

    # Simulation needs only the schematic — it reads the subcircuits the
    # analyzer detected, not the copper. The PCB is an optional refinement:
    # with traces, the testbenches carry their parasitics.
    if spice and sch_json:
        analyzers["spice"] = _run_spice(
            Path(analyzers["schematic"]["report_json"]),
            review_dir,
            pcb_json=(
                Path(analyzers["pcb"]["report_json"])
                if analyzers["pcb"].get("report_json")
                else None
            ),
        )

    # Emitter cross-checks need the BOM the board was generated from.
    bom_path = _p("bom")
    bom = schema.load_json(bom_path) if bom_path.exists() else {}

    da_path = _p("design_artifact.deterministic")
    design_artifact = schema.load_json(da_path) if da_path.exists() else {}

    hdm_path = _p("hdm", "yaml")
    hdm = yaml.safe_load(hdm_path.read_text(encoding="utf-8")) or {} if hdm_path.exists() else {}
    placeholders = _placeholder_check(hdm)
    test_points = ((hdm.get("synthesis") or {}).get("test_points") or {}) if isinstance(hdm, dict) else {}

    emitter_report_path = _p("emitter_report")
    pwr_flag_nets: set[str] = set()
    if emitter_report_path.exists():
        pwr_flag_nets = set(schema.load_json(emitter_report_path).get("pwr_flag_nets", []))

    autoroute_path = _p("autoroute_report")
    autoroute = _load(str(autoroute_path)) if autoroute_path.exists() else {}
    if not autoroute:
        autoroute = {
            "attempted": False,
            "ok": False,
            "reason": "no autoroute step ran for this board (run `blpl autoroute` or `blpl run`)",
        }
    elif pcb_path is not None and autoroute.get("pcb") and Path(autoroute["pcb"]).name != pcb_path.name:
        # The record is real but it is about an earlier compile. Treating it as
        # this board's would turn every unrouted net on a fresh, never-routed
        # emission into a "router could not finish" finding.
        autoroute = {
            "attempted": False,
            "ok": False,
            "reason": (
                f"the last autoroute ran on {Path(autoroute['pcb']).name}, not on this "
                f"compile ({pcb_path.name}); run `blpl autoroute` again"
            ),
        }

    crosschecks = _stage1_crosscheck(design_artifact, bom)
    crosschecks += _emitter_crosschecks(bom, sch_json, pcb_json, hdm, project_dir, board)
    emitter_evidence = {c["check"]: True for c in crosschecks}

    ctx = _Context(
        pwr_flag_nets=pwr_flag_nets,
        autoroute=autoroute,
        test_points=test_points,
        lifecycle_reason=lifecycle_reason,
        emitter_evidence=emitter_evidence,
    )

    # Partition every analyzer finding by provenance.
    emitter_defects: list[dict] = list(crosschecks)
    design_issues: list[dict] = []
    expected_counts: dict[str, int] = {}

    for source in ("schematic", "pcb", "cross", "emc", "spice"):
        data = _load(analyzers.get(source, {}).get("report_json"))
        for f in data.get("findings", []):
            provenance, owner = _classify(f, ctx)
            record = {
                "source": source,
                "rule_id": f.get("rule_id"),
                "severity": f.get("severity", "info"),
                "summary": f.get("summary", ""),
                "recommendation": _test_point_recommendation(f, test_points),
                "components": f.get("components", []),
                "nets": f.get("nets", []),
            }
            if provenance == "emitter":
                record["owner"] = owner
                # kicad-happy assumes a human drew the board, so its advice is
                # things like "Tools > Update PCB from Schematic". On a generated
                # board that is the wrong instruction to the wrong person — the
                # fix lives in the emitter. Say so instead.
                rule_id = f.get("rule_id", "")
                if rule_id in _EMITTER_RULES:
                    record["recommendation"] = _EMITTER_RULES[rule_id][1]
                emitter_defects.append(record)
            elif provenance == "design":
                if f.get("rule_id") == "RT-001":
                    record["recommendation"] = (
                        "Freerouting left this net open. Route it interactively; if it is one "
                        "of many, the board is congested where it sits."
                    )
                # Info-level findings are detections ("Ethernet PHY U3 found"),
                # not problems. Keep them out of the actionable list.
                if record["severity"] in ("error", "warning"):
                    design_issues.append(record)
            else:
                rid = f.get("rule_id", "")
                expected_counts[rid] = expected_counts.get(rid, 0) + 1

    expected = [{"rule_id": k, "count": v} for k, v in sorted(expected_counts.items())]

    _SEV = {"error": 0, "warning": 1, "info": 2}
    design_issues.sort(key=lambda d: _SEV.get(d["severity"], 3))

    report = {
        # A board is not "ok" if the pipeline mis-emitted it, and it is certainly
        # not ok if parts of it are stand-ins. Emitter defects are ours to fix and
        # they gate; so do placeholders, which are the last thing standing between
        # a generated board and a fab house. Design issues are the user's to triage.
        "ok": not emitter_defects and not placeholders,
        "skipped": False,
        "board": board,
        "kicad_happy": str(base),
        "summary": {
            "emitter": len(emitter_defects),
            "design": len(design_issues),
            "expected": sum(expected_counts.values()),
            "placeholders": len(placeholders),
        },
        "placeholders": placeholders,
        # Hoisted out of analyzers[] because it is the one analyzer whose
        # *not having run* is a routine, actionable state rather than a broken
        # checkout — the reader needs to see it without digging.
        "simulation": analyzers.get("spice", {"skipped": True, "reason": "not run"}),
        "autoroute": autoroute,
        "lifecycle": {
            "ran": bool(lifecycle_args),
            "reason": lifecycle_reason,
        },
        "test_points": test_points,
        "emitter_defects": emitter_defects,
        "design_issues": design_issues,
        "expected": expected,
        "expected_reasons": ctx.reasons,
        "analyzers": analyzers,
    }

    _p("review_report").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _p("review", "md").write_text(_render_markdown(report), encoding="utf-8")
    return report
