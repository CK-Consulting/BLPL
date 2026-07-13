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
  expected  — a known, accepted consequence of what BLPL does not do yet. There
              is no autorouter, so every net is unrouted. Reporting these as
              failures would train the user to ignore the report.

Classifying provenance is the whole point: an unclassified review of a generated
board is ~90% noise, because the generator's own limitations swamp the real
findings.

Analyzer availability is probed at runtime; a missing kicad-happy checkout
produces a `skipped` result, not a failure — same contract as Stage 7.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import schema

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

# Known BLPL limitations. Real findings, but not actionable until the roadmap
# catches up — surfacing them as failures would make the report untrustworthy.
_EXPECTED_RULES: dict[str, str] = {
    "RT-001": "BLPL has no autorouter (roadmap phase 5); every net is unrouted by construction.",
    "TE-001": "BLPL does not synthesize test points.",
    "LC-007": "Lifecycle audit is opt-in (needs network + distributor API keys).",
    "DS-001": "Datasheet corpus is not synced by default.",
}


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


def _run_analyzer(script: Path, args: list[str], out_path: Path) -> dict:
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


def _load(path: str | None) -> dict:
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _classify(finding: dict, emitter_evidence: dict) -> tuple[str, str | None]:
    """Return (provenance, owner) for one analyzer finding."""
    rule = finding.get("rule_id", "")
    if rule in _EXPECTED_RULES:
        return "expected", None
    if rule in _EMITTER_RULES:
        owner, _ = _EMITTER_RULES[rule]
        # SS-001 only indicts the emitter if the BOM actually had MPNs to lose.
        if rule == "SS-001" and not emitter_evidence.get("mpn_dropped"):
            return "design", None
        return "emitter", owner
    return "design", None


def _emitter_crosschecks(bom: dict, sch: dict, pcb: dict) -> list[dict]:
    """Compare what the BOM promised against what the emitter actually wrote.

    These checks are invisible to kicad-happy: it only ever sees the emitted
    files, so it cannot know a part was supposed to be there and isn't.
    """
    checks: list[dict] = []
    rows = bom.get("rows", [])
    if not rows:
        return checks

    bom_count = len(rows)
    bom_with_mpn = sum(1 for r in rows if r.get("mpn"))

    sch_stats = sch.get("statistics", {})
    sch_count = sch_stats.get("total_components", 0)
    sch_with_mpn = sch.get("bom_lock", {}).get("components_with_mpn", 0)

    pcb_count = len(pcb.get("footprints", []))

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

    if sch_count and sch_count < bom_count:
        checks.append(
            {
                "check": "symbol_leakage",
                "severity": "error",
                "owner": "emitter/sch.py",
                "summary": f"bom.json has {bom_count} rows but only {sch_count} symbols were emitted.",
                "recommendation": "Components are being dropped between Stage 5 and Stage 6.",
            }
        )

    if pcb_count and pcb_count < bom_count:
        checks.append(
            {
                "check": "footprint_leakage",
                "severity": "error",
                "owner": "emitter/pcb.py",
                "summary": f"bom.json has {bom_count} rows but only {pcb_count} footprints were placed.",
                "recommendation": (
                    "Rows without a resolved footprint_hint are silently skipped. They should "
                    "surface as a Stage 2/3 coverage miss instead of vanishing here."
                ),
            }
        )

    return checks


def _render_markdown(report: dict) -> str:
    lines = ["# Stage 8 — Design Review", ""]
    counts = report["summary"]
    lines.append(
        f"**{counts['emitter']} emitter defects** · "
        f"**{counts['design']} design issues** · "
        f"{counts['expected']} expected (known BLPL limitations)"
    )
    lines.append("")

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
        lines.append("")

    if report["expected"]:
        lines += ["## Expected — known BLPL limitations", ""]
        for d in report["expected"]:
            why = _EXPECTED_RULES.get(d.get("rule_id", ""), "")
            lines.append(f"- {d.get('rule_id','')} ×{d['count']} — {why}")
        lines.append("")

    return "\n".join(lines) + "\n"


def run(
    project_dir: Path,
    *,
    sch_path: Path | None = None,
    pcb_path: Path | None = None,
    emc: bool = True,
) -> dict:
    """Run Stage 8 and write review_report.json + review.md into .pipeline/."""
    project_dir = Path(project_dir)
    pipeline_dir = project_dir / ".pipeline"
    review_dir = pipeline_dir / "review"
    review_dir.mkdir(parents=True, exist_ok=True)

    base = _find_kicad_happy()
    if base is None:
        report = {
            "ok": True,
            "skipped": True,
            "reason": (
                "kicad-happy not found. Expected at <repo>/kicad-happy "
                "(git submodule update --init) or set BLPL_KICAD_HAPPY."
            ),
        }
        (pipeline_dir / "review_report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        return report

    kicad_scripts = base / "skills" / "kicad" / "scripts"
    emc_scripts = base / "skills" / "emc" / "scripts"

    analyzers: dict[str, dict] = {}

    if sch_path is not None and sch_path.exists():
        analyzers["schematic"] = _run_analyzer(
            kicad_scripts / "analyze_schematic.py", [str(sch_path)], review_dir / "schematic.json"
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

    # Emitter cross-checks need the BOM the board was generated from.
    bom_path = pipeline_dir / "bom.json"
    bom = schema.load_json(bom_path) if bom_path.exists() else {}
    crosschecks = _emitter_crosschecks(bom, sch_json, pcb_json)
    emitter_evidence = {c["check"]: True for c in crosschecks}

    # Partition every analyzer finding by provenance.
    emitter_defects: list[dict] = list(crosschecks)
    design_issues: list[dict] = []
    expected_counts: dict[str, int] = {}

    for source in ("schematic", "pcb", "cross", "emc"):
        data = _load(analyzers.get(source, {}).get("report_json"))
        for f in data.get("findings", []):
            provenance, owner = _classify(f, emitter_evidence)
            record = {
                "source": source,
                "rule_id": f.get("rule_id"),
                "severity": f.get("severity", "info"),
                "summary": f.get("summary", ""),
                "recommendation": f.get("recommendation"),
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
        # A board is not "ok" if the pipeline mis-emitted it. Design issues are
        # the user's to triage; emitter defects are ours, and they gate.
        "ok": not emitter_defects,
        "skipped": False,
        "kicad_happy": str(base),
        "summary": {
            "emitter": len(emitter_defects),
            "design": len(design_issues),
            "expected": sum(expected_counts.values()),
        },
        "emitter_defects": emitter_defects,
        "design_issues": design_issues,
        "expected": expected,
        "analyzers": analyzers,
    }

    (pipeline_dir / "review_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (pipeline_dir / "review.md").write_text(_render_markdown(report), encoding="utf-8")
    return report
