"""Pipeline orchestrator CLI.

Subcommands wired:
  stage0-det       — deterministic Markdown -> design_artifact.json (no LLM)
  stage0-llm       — LLM Markdown -> design_artifact.json
  stage0-compare   — diff two design_artifact.json files (deterministic vs llm)
  stage1           — LLM design_artifact -> bom.json
  stage2           — scan KiCad libraries for coverage of a bom.json
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import (
    llm_adapter,
    schema,
    stage0_compare,
    stage0_deterministic,
    stage0_llm,
    stage1_resolve_bom,
    stage2_library_lookup,
    stage3_generate,
    stage4_synthesize_nets,
    stage5_emit_yaml_hdm,
    stage6_compile_kicad,
    stage7_validate,
    stage8_review,
)


_STAGE_ORDER = [
    "stage0",
    "stage1",
    "stage2",
    "stage3",
    "stage4",
    "stage5",
    "stage6",
    "stage7",
    "stage8",
]


# Three levels up from blpl/core/cli.py lands us at board-layer-pipe-line/,
# where the kicad-symbols/kicad-footprints submodules live (symlinked for now).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SYMBOLS = _REPO_ROOT / "kicad-symbols"
_DEFAULT_FOOTPRINTS = _REPO_ROOT / "kicad-footprints"


def _project_dir(args: argparse.Namespace) -> Path:
    p = Path(args.project_dir).resolve()
    (p / ".pipeline").mkdir(parents=True, exist_ok=True)
    return p


def _md_inputs(project_dir: Path) -> list[Path]:
    """Return the Markdown input files for a project.

    Default: all *.md files directly under project_dir, excluding anything under
    .pipeline/ or .history/ subdirs.
    """
    files = [p for p in sorted(project_dir.glob("*.md"))]
    return files


def _make_adapter(args: argparse.Namespace) -> llm_adapter.LLMAdapter:
    return llm_adapter.get_adapter(provider=args.llm_provider, model=args.llm_model)


def _cmd_stage0_det(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    md_files = _md_inputs(proj)
    if not md_files:
        print(f"error: no .md files found under {proj}", file=sys.stderr)
        return 2
    out = proj / ".pipeline" / "design_artifact.deterministic.json"
    artifact = stage0_deterministic.run(md_files, out)
    print(
        f"stage0-det: {len(artifact['components'])} components, "
        f"{len(artifact['connectors'])} connectors  ({len(md_files)} .md files)"
    )
    print(f"            wrote {out}")
    return 0


def _cmd_stage0_llm(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    md_files = _md_inputs(proj)
    if not md_files:
        print(f"error: no .md files found under {proj}", file=sys.stderr)
        return 2
    adapter = _make_adapter(args)
    out = proj / ".pipeline" / "design_artifact.llm.json"
    print(f"stage0-llm: using {adapter.provider}/{adapter.model} on {len(md_files)} files…", file=sys.stderr)
    artifact = stage0_llm.run(md_files, out, adapter=adapter)
    print(
        f"stage0-llm: {len(artifact['components'])} components, "
        f"{len(artifact['connectors'])} connectors"
    )
    print(f"            wrote {out}")
    return 0


def _cmd_stage0_compare(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    a = proj / ".pipeline" / "design_artifact.deterministic.json"
    b = proj / ".pipeline" / "design_artifact.llm.json"
    if not a.exists() or not b.exists():
        missing = [str(p) for p in (a, b) if not p.exists()]
        print(f"error: missing input artifact(s): {missing}", file=sys.stderr)
        print("       run stage0-det and stage0-llm first", file=sys.stderr)
        return 2
    out = proj / ".pipeline" / "design_artifact.compare.md"
    diff = stage0_compare.run(a, b, out)
    c = diff["components"]
    print(
        f"stage0-compare: components only_det={len(c['only_a'])} only_llm={len(c['only_b'])} "
        f"in_both={len(c['both'])} disagreements={len(c['disagreements'])} "
        f"agreement_rate={c['agreement_rate']}"
    )
    print(f"                wrote {out}")
    return 0


def _cmd_stage1(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    source = args.source  # "det" | "llm"
    src_path = proj / ".pipeline" / f"design_artifact.{source}.json"
    if source == "det":
        src_path = proj / ".pipeline" / "design_artifact.deterministic.json"
    if not src_path.exists():
        print(f"error: {src_path} does not exist (run stage0-{source} first)", file=sys.stderr)
        return 2
    out = proj / ".pipeline" / "bom.json"
    adapter = _make_adapter(args)
    print(f"stage1: resolving BOM from {src_path.name} via {adapter.provider}/{adapter.model}…", file=sys.stderr)
    bom = stage1_resolve_bom.run(src_path, out, adapter=adapter)
    low = stage1_resolve_bom.low_confidence_rows(bom)
    print(f"stage1: {len(bom['rows'])} rows ({len(low)} low-confidence, need review)")
    print(f"        wrote {out}")
    return 0


def _cmd_stage3(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    coverage = proj / ".pipeline" / "coverage_report.json"
    bom = proj / ".pipeline" / "bom.json"
    for p, label in [(coverage, "coverage_report"), (bom, "bom")]:
        if not p.exists():
            print(f"error: {label} missing at {p}", file=sys.stderr)
            return 2
    result = stage3_generate.run(
        coverage_path=coverage,
        bom_path=bom,
        project_dir=proj,
        auto_generate=args.auto_fill_gaps,
    )
    auto_count = sum(1 for g in result["gaps"] if g["auto_generated"])
    pending = len(result["gaps"]) - auto_count
    print(f"stage3: {auto_count} auto-generated, {pending} pending user input")
    print(f"        wrote {stage3_generate.paths_for(proj).gaps_md_path}")
    return 0 if pending == 0 else 1


def _cmd_stage1_synthesize_connectors(args: argparse.Namespace) -> int:
    """Append synthesized connector rows to an existing bom.json (no LLM call)."""
    proj = _project_dir(args)
    artifact_path = proj / ".pipeline" / f"design_artifact.{args.source}.json"
    if args.source == "det":
        artifact_path = proj / ".pipeline" / "design_artifact.deterministic.json"
    bom_path = proj / ".pipeline" / "bom.json"
    for p, label in [(artifact_path, "design_artifact"), (bom_path, "bom")]:
        if not p.exists():
            print(f"error: {label} missing at {p}", file=sys.stderr)
            return 2
    before = len(schema.load_json(bom_path)["rows"])
    bom = stage1_resolve_bom.apply_connector_synthesis(artifact_path, bom_path)
    after = len(bom["rows"])
    added = after - before
    print(
        f"stage1-synthesize-connectors: appended {added} connector row(s); "
        f"bom now has {after} rows."
    )
    return 0


_DEFAULT_KICAD_PYTHON_CANDIDATES = (
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3",
    "/usr/lib/kicad/bin/python3",
    "/opt/kicad/bin/python3",
)

_PLUGIN_PACKAGE_NAME = "blpl.plugin_kicad"


def _resolve_kicad_python(cli_value: str | None) -> Path | None:
    """Find the Python interpreter bundled with KiCad (has ``pcbnew`` importable).

    Resolution order: explicit CLI flag > HDM_KICAD_PYTHON env var > platform
    default paths > ``kicad-python`` on PATH. Returns ``None`` if nothing
    suitable is found (caller prints an actionable error).
    """
    if cli_value:
        p = Path(cli_value)
        return p if p.exists() else None
    envval = os.environ.get("HDM_KICAD_PYTHON")
    if envval:
        p = Path(envval)
        if p.exists():
            return p
    for candidate in _DEFAULT_KICAD_PYTHON_CANDIDATES:
        p = Path(candidate)
        if p.exists():
            return p
    which = shutil.which("kicad-python")
    if which:
        return Path(which)
    return None


def _cmd_stage6_plugin(args: argparse.Namespace) -> int:
    """Run the PCB plugin in standalone mode via KiCad's bundled Python."""
    proj = _project_dir(args)
    hdm_path = proj / ".pipeline" / "hdm.yaml"
    if not hdm_path.exists():
        print(f"error: hdm.yaml missing at {hdm_path} — run stage5 first", file=sys.stderr)
        return 2

    kicad_python = _resolve_kicad_python(args.kicad_python)
    if kicad_python is None:
        print(
            "error: could not find KiCad's bundled Python. Pass --kicad-python or set "
            "HDM_KICAD_PYTHON to the path of the python3 shipped with KiCad (on macOS: "
            "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3).",
            file=sys.stderr,
        )
        return 2

    # Timestamp shared with the sch + pro the emitter already produced.
    from datetime import datetime, timezone

    stamp = args.stamp or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
    import re as _re

    project_name = proj.name or "project"
    base = _re.sub(r"[^A-Za-z0-9._-]+", "_", project_name.strip()) or "project"
    out_path = proj / ".pipeline" / f"{base}_{stamp}.kicad_pcb"

    # The plugin lives inside blpl/; PYTHONPATH needs the parent so
    # `-m blpl.plugin_kicad.build_pcb` resolves under KiCad's Python.
    blpl_parent = _REPO_ROOT
    footprints_root = args.footprints_root or str(_DEFAULT_FOOTPRINTS)

    # KiCad's bundled Python doesn't ship with pyyaml; pre-convert to JSON so
    # the plugin reads via the stdlib json module.
    import json as _json
    import yaml as _yaml

    hdm_json_path = proj / ".pipeline" / "hdm.plugin.json"
    with hdm_path.open() as f:
        hdm_data = _yaml.safe_load(f) or {}
    with hdm_json_path.open("w") as f:
        _json.dump(hdm_data, f, indent=2)

    cmd = [
        str(kicad_python),
        "-m",
        f"{_PLUGIN_PACKAGE_NAME}.build_pcb",
        "--hdm",
        str(hdm_json_path),
        "--out",
        str(out_path),
        "--footprints-root",
        str(footprints_root),
    ]
    env = os.environ.copy()
    # Prepend the repo root so `-m blpl.plugin_kicad.build_pcb` resolves. We
    # intentionally don't touch KiCad's own site-packages — adding to
    # PYTHONPATH composes.
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{blpl_parent}{os.pathsep}{existing}" if existing else str(blpl_parent)

    print(f"stage6-plugin: using {kicad_python}", file=sys.stderr)
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        return result.returncode
    print(f"stage6-plugin: wrote {out_path}", file=sys.stderr)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Launch the FastAPI backend. Requires the 'webapp' extras."""
    try:
        import uvicorn  # type: ignore[import-not-found]
    except ImportError:
        print(
            "error: uvicorn not installed. Install the webapp extras with: "
            "uv pip install -e '.[webapp]'",
            file=sys.stderr,
        )
        return 2

    # Pre-set the env so main.py picks up the workspace root at import time.
    if args.workspace:
        os.environ["BLPL_WORKSPACE"] = str(Path(args.workspace).resolve())

    url = f"http://{args.host}:{args.port}"
    print(f"blpl serve: starting on {url} (workspace={os.environ.get('BLPL_WORKSPACE', 'auto')})", file=sys.stderr)

    if not args.no_browser:
        import webbrowser
        import threading
        import time

        def _open_later() -> None:
            time.sleep(1.0)  # give uvicorn a moment to bind
            webbrowser.open(url)

        threading.Thread(target=_open_later, daemon=True).start()

    uvicorn.run(
        "blpl.webapp.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


def _cmd_resolve_pin_map(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    bom = proj / ".pipeline" / "bom.json"
    if not bom.exists():
        print(f"error: bom.json missing at {bom}", file=sys.stderr)
        return 2
    try:
        result = stage3_generate.resolve_pin_map(
            local_id=args.local_id,
            bom_path=bom,
            lib_symbol=args.lib_symbol,
            csv_path=Path(args.csv) if args.csv else None,
            symbols_root=Path(args.symbols_root) if args.symbols_root else None,
            dry_run=args.dry_run,
        )
    except stage3_generate.ResolvePinMapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    action = "would write" if args.dry_run else "wrote"
    print(
        f"resolve-pin-map: {action} pin_map for {result['local_id']} "
        f"(source={result['source']}, {len(result['pin_map'])} entries)"
    )
    for signal, pin in list(result["pin_map"].items())[:20]:
        print(f"        {signal:<12} -> {pin}")
    if len(result["pin_map"]) > 20:
        print(f"        ... ({len(result['pin_map']) - 20} more)")
    for w in result["warnings"]:
        print(f"        warning: {w}", file=sys.stderr)
    return 0


def _cmd_stage4(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    da_path = proj / ".pipeline" / f"design_artifact.{args.source}.json"
    if args.source == "det":
        da_path = proj / ".pipeline" / "design_artifact.deterministic.json"
    bom_path = proj / ".pipeline" / "bom.json"
    if not da_path.exists():
        print(f"error: {da_path} missing — run stage0-{args.source} first", file=sys.stderr)
        return 2
    out = proj / ".pipeline" / "nets.json"
    nets = stage4_synthesize_nets.run(
        design_artifact_path=da_path,
        bom_path=bom_path if bom_path.exists() else None,
        output_path=out,
    )
    total_members = sum(len(n["members"]) for n in nets["nets"])
    diff_pairs = sum(1 for n in nets["nets"] if "diff_pair_of" in n)
    print(f"stage4: {len(nets['nets'])} nets ({total_members} pad members, {diff_pairs // 2} diff pairs)")
    print(f"        wrote {out}")
    return 0


def _cmd_stage5(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    da_path = proj / ".pipeline" / "design_artifact.deterministic.json"
    if args.source == "llm":
        da_path = proj / ".pipeline" / "design_artifact.llm.json"
    bom_path = proj / ".pipeline" / "bom.json"
    nets_path = proj / ".pipeline" / "nets.json"
    proj_cfg = proj / ".pipeline" / "project.yaml"
    coverage = proj / ".pipeline" / "coverage_report.json"

    for p, label in [(da_path, "design_artifact"), (bom_path, "bom"), (nets_path, "nets")]:
        if not p.exists():
            print(f"error: {label} missing at {p}", file=sys.stderr)
            return 2

    out = proj / ".pipeline" / "hdm.yaml"
    try:
        hdm = stage5_emit_yaml_hdm.run(
            bom_path=bom_path,
            nets_path=nets_path,
            design_artifact_path=da_path,
            project_config_path=proj_cfg,
            output_path=out,
            coverage_path=coverage if coverage.exists() else None,
        )
    except stage5_emit_yaml_hdm.MissingProjectConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"stage5: emitted HDM with {len(hdm.get('components', {}))} components, {len(hdm.get('nets', {}))} nets")
    print(f"        wrote {out}")
    return 0


def _cmd_stage2(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    bom_path = proj / ".pipeline" / "bom.json"
    output_path = proj / ".pipeline" / "coverage_report.json"
    if not bom_path.exists():
        print(f"error: no bom.json at {bom_path}", file=sys.stderr)
        return 2
    report = stage2_library_lookup.run(
        bom_path=bom_path,
        symbols_root=Path(args.symbols_root).resolve(),
        footprints_root=Path(args.footprints_root).resolve(),
        output_path=output_path,
    )
    s = report["summary"]
    print(f"stage2: {s['hit']} hit / {s['needs_variant']} needs_variant / {s['miss']} miss (of {s['total']})")
    print(f"        wrote {output_path}")
    return 0 if s["miss"] == 0 else 1


def _stage_index(name: str) -> int:
    return _STAGE_ORDER.index(name)


def _cmd_run(args: argparse.Namespace) -> int:
    """Orchestrator: run each stage in order, honouring --from / --to and halt-on-error semantics."""
    start = _stage_index(args.start_stage)
    end = _stage_index(args.end_stage)
    if start > end:
        print(f"error: --from {args.start_stage} is after --to {args.end_stage}", file=sys.stderr)
        return 2

    def _attempt(stage: str, fn, note: str = "") -> int:
        print(f"==> {stage}{' ' + note if note else ''}", file=sys.stderr)
        rc = fn()
        if rc != 0 and not args.continue_on_error:
            print(f"==> {stage} returned {rc}; halting (use --continue-on-error to proceed)", file=sys.stderr)
        return rc

    # Thin namespaces reused for the per-stage command functions.
    base = argparse.Namespace(
        project_dir=args.project_dir,
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
    )

    for i in range(start, end + 1):
        stage = _STAGE_ORDER[i]
        if stage == "stage0":
            rc = _attempt("stage0-det", lambda: _cmd_stage0_det(base))
            if rc != 0 and not args.continue_on_error:
                return rc
            if args.stage0_mode in ("llm", "both"):
                rc = _attempt("stage0-llm", lambda: _cmd_stage0_llm(base))
                if rc != 0 and not args.continue_on_error:
                    return rc
            if args.stage0_mode == "both":
                rc = _attempt("stage0-compare", lambda: _cmd_stage0_compare(base))
                if rc != 0 and not args.continue_on_error:
                    return rc
        elif stage == "stage1":
            ns = argparse.Namespace(**vars(base), source="llm" if args.stage0_mode in ("llm", "both") else "det")
            rc = _attempt("stage1", lambda: _cmd_stage1(ns))
            if rc != 0 and not args.continue_on_error:
                return rc
        elif stage == "stage2":
            ns = argparse.Namespace(**vars(base), symbols_root=args.symbols_root, footprints_root=args.footprints_root)
            rc = _attempt("stage2", lambda: _cmd_stage2(ns))
            if rc != 0 and not args.continue_on_error:
                return rc
        elif stage == "stage3":
            ns = argparse.Namespace(**vars(base), auto_fill_gaps=args.auto_fill_gaps)
            rc = _attempt("stage3", lambda: _cmd_stage3(ns))
            if rc != 0 and not args.continue_on_error:
                return rc
        elif stage == "stage4":
            ns = argparse.Namespace(**vars(base), source="llm" if args.stage0_mode in ("llm", "both") else "det")
            rc = _attempt("stage4", lambda: _cmd_stage4(ns))
            if rc != 0 and not args.continue_on_error:
                return rc
        elif stage == "stage5":
            ns = argparse.Namespace(**vars(base), source="llm" if args.stage0_mode in ("llm", "both") else "det")
            rc = _attempt("stage5", lambda: _cmd_stage5(ns))
            if rc != 0 and not args.continue_on_error:
                return rc
        elif stage == "stage6":
            rc = _attempt("stage6", lambda: _cmd_stage6(base))
            if rc != 0 and not args.continue_on_error:
                return rc
        elif stage == "stage7":
            rc = _attempt("stage7", lambda: _cmd_stage7(base))
            if rc != 0 and not args.continue_on_error:
                return rc
        elif stage == "stage8":
            ns = argparse.Namespace(**vars(base), no_emc=False)
            rc = _attempt("stage8", lambda: _cmd_stage8(ns))
            if rc != 0 and not args.continue_on_error:
                return rc
    return 0


def _cmd_stage6(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    hdm = proj / ".pipeline" / "hdm.yaml"
    if not hdm.exists():
        print(f"error: hdm.yaml missing at {hdm} — run stage5 first", file=sys.stderr)
        return 2
    outputs = stage6_compile_kicad.run(hdm, proj / ".pipeline")
    file_outputs = {k: v for k, v in outputs.items() if k != "base"}
    print(f"stage6: compiled KiCad project ({len(file_outputs)} files, base={outputs['base']})")
    for kind, path in file_outputs.items():
        print(f"        {kind}: {path}")
    return 0


def _cmd_stage7(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    pipeline_dir = proj / ".pipeline"
    # Pick the most recent timestamped compile when several exist.
    pcb_candidates = sorted(pipeline_dir.glob("*.kicad_pcb"), reverse=True)
    sch_candidates = sorted(pipeline_dir.glob("*.kicad_sch"), reverse=True)
    pcb = pcb_candidates[0] if pcb_candidates else None
    sch = sch_candidates[0] if sch_candidates else None
    report = stage7_validate.run(
        proj,
        pcb_path=pcb,
        sch_path=sch,
        generated_symbols_dir=proj / "generated" / "symbols",
        coverage_path=pipeline_dir / "coverage_report.json",
    )
    status = "PASS" if report["ok"] else "FAIL"

    def _label(r: dict) -> str:
        if r.get("skipped"):
            return "skipped"
        if r["ok"]:
            return "ok"
        return f"fail (exit={r.get('exit_code')})"

    klc_label = _label(report["klc_symbols"])
    erc_label = _label(report["erc"])
    drc_label = _label(report["drc"])
    cov = report["coverage"]
    cov_label = "skipped" if cov.get("skipped") else f"{cov.get('hit',0)} hit / {cov.get('miss',0)} miss"
    print(f"stage7: {status}  klc={klc_label}  erc={erc_label}  drc={drc_label}  coverage={cov_label}")
    print(f"        wrote {pipeline_dir / 'validation_report.json'}")
    return 0 if report["ok"] else 1


def _cmd_stage8(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    pipeline_dir = proj / ".pipeline"
    # Same "most recent timestamped compile wins" rule as stage7.
    pcb_candidates = sorted(pipeline_dir.glob("*.kicad_pcb"), reverse=True)
    sch_candidates = sorted(pipeline_dir.glob("*.kicad_sch"), reverse=True)
    pcb = pcb_candidates[0] if pcb_candidates else None
    sch = sch_candidates[0] if sch_candidates else None
    if pcb is None and sch is None:
        print(
            f"error: no emitted KiCad files in {pipeline_dir} — run stage6 first",
            file=sys.stderr,
        )
        return 2

    report = stage8_review.run(
        proj,
        sch_path=sch,
        pcb_path=pcb,
        emc=not args.no_emc,
    )
    if report.get("skipped"):
        print(f"stage8: skipped — {report['reason']}")
        return 0

    s = report["summary"]
    status = "PASS" if report["ok"] else "FAIL"
    print(
        f"stage8: {status}  emitter={s['emitter']}  design={s['design']}  expected={s['expected']}"
    )
    for d in report["emitter_defects"]:
        rule = d.get("rule_id") or d.get("check")
        print(f"        [emitter] {rule}: {d['summary']}")
    print(f"        wrote {pipeline_dir / 'review.md'}")
    # Emitter defects gate; design issues are the user's to triage.
    return 0 if report["ok"] else 1


def _add_llm_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--llm-provider", default=None, help="anthropic | openai | ollama (falls back to HDM_LLM_PROVIDER env)")
    p.add_argument("--llm-model", default=None, help="provider-specific model ID (falls back to HDM_LLM_MODEL env)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blpl")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stage0-det", help="Deterministic Markdown -> design_artifact.json.")
    p.add_argument("--project-dir", required=True)
    p.set_defaults(func=_cmd_stage0_det)

    p = sub.add_parser("stage0-llm", help="LLM Markdown -> design_artifact.json.")
    p.add_argument("--project-dir", required=True)
    _add_llm_flags(p)
    p.set_defaults(func=_cmd_stage0_llm)

    p = sub.add_parser("stage0-compare", help="Diff deterministic vs LLM design_artifact outputs.")
    p.add_argument("--project-dir", required=True)
    p.set_defaults(func=_cmd_stage0_compare)

    p = sub.add_parser("stage1", help="LLM design_artifact -> bom.json.")
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--source",
        default="det",
        choices=("det", "llm"),
        help="Which Stage 0 output to feed into Stage 1 (default: deterministic).",
    )
    _add_llm_flags(p)
    p.set_defaults(func=_cmd_stage1)

    p = sub.add_parser(
        "stage1-synthesize-connectors",
        help="Append synthesized BOM rows for every connector in design_artifact not already in bom.json (no LLM call).",
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--source",
        default="det",
        choices=("det", "llm"),
        help="Which Stage 0 artifact to read connectors from (default: deterministic).",
    )
    p.set_defaults(func=_cmd_stage1_synthesize_connectors)

    p = sub.add_parser("stage2", help="Scan KiCad libraries for symbol/footprint coverage.")
    p.add_argument("--project-dir", required=True)
    p.add_argument("--symbols-root", default=str(_DEFAULT_SYMBOLS))
    p.add_argument("--footprints-root", default=str(_DEFAULT_FOOTPRINTS))
    p.set_defaults(func=_cmd_stage2)

    p = sub.add_parser("stage6", help="Compile hdm.yaml to .kicad_pcb + .kicad_pro via yaml_to_kicad.py.")
    p.add_argument("--project-dir", required=True)
    p.set_defaults(func=_cmd_stage6)

    p = sub.add_parser(
        "stage6-plugin",
        help="Build .kicad_pcb via the KiCad plugin (pcbnew Python API). Requires KiCad installed.",
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--kicad-python",
        default=None,
        help="Path to the Python interpreter bundled with KiCad. Defaults to the "
        "HDM_KICAD_PYTHON env var, then platform-standard locations.",
    )
    p.add_argument(
        "--footprints-root",
        default=None,
        help="Override the kicad-footprints library root (defaults to the bundled submodule).",
    )
    p.add_argument(
        "--stamp",
        default=None,
        help="Timestamp slug for the output filename; auto-generated if omitted.",
    )
    p.set_defaults(func=_cmd_stage6_plugin)

    p = sub.add_parser("stage7", help="Run KLC + DRC + coverage validation, write validation_report.json.")
    p.add_argument("--project-dir", required=True)
    p.set_defaults(func=_cmd_stage7)

    p = sub.add_parser(
        "stage8",
        help="Design review (kicad-happy): schematic + PCB + EMC analysis, write review_report.json.",
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--no-emc",
        action="store_true",
        help="Skip the EMC rule pass (the slowest analyzer).",
    )
    p.set_defaults(func=_cmd_stage8)

    p = sub.add_parser("run", help="Run the full pipeline end-to-end (stages 0–8).")
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--from",
        dest="start_stage",
        default="stage0",
        choices=_STAGE_ORDER,
        help="Stage to start from (default: stage0).",
    )
    p.add_argument(
        "--to",
        dest="end_stage",
        default="stage8",
        choices=_STAGE_ORDER,
        help="Stage to stop after (default: stage8).",
    )
    p.add_argument(
        "--stage0",
        dest="stage0_mode",
        default="det",
        choices=("det", "llm", "both"),
        help="Stage 0 mode: det (deterministic, default), llm, or both (runs det + llm + compare).",
    )
    p.add_argument("--auto-fill-gaps", action="store_true", help="Pass through to stage3.")
    _add_llm_flags(p)
    p.add_argument(
        "--symbols-root", default=str(_DEFAULT_SYMBOLS), help="Pass through to stage2."
    )
    p.add_argument(
        "--footprints-root", default=str(_DEFAULT_FOOTPRINTS), help="Pass through to stage2."
    )
    p.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep running subsequent stages even if a stage returns non-zero (e.g. stage2 misses, stage3 pending prompts).",
    )
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser(
        "serve",
        help="Launch the BLPL FastAPI backend on localhost (requires webapp extras).",
    )
    p.add_argument(
        "--workspace",
        default=None,
        help="Root directory to scan for BLPL projects. Defaults to BLPL_WORKSPACE env or the repo root.",
    )
    p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1 — local only).")
    p.add_argument("--port", type=int, default=7878, help="Bind port (default: 7878).")
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open a browser window on startup.",
    )
    p.add_argument(
        "--reload",
        action="store_true",
        help="Uvicorn dev mode: auto-reload on code changes.",
    )
    p.add_argument(
        "--log-level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
    )
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("stage3", help="Emit gap prompts (and optionally auto-generate generic symbols).")
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--auto-fill-gaps",
        action="store_true",
        help="Also auto-generate generic rectangular symbols for rows whose pin_count is known. Off by default per decision #3.",
    )
    p.set_defaults(func=_cmd_stage3)

    p = sub.add_parser(
        "resolve-pin-map",
        help="Fill or overwrite a single BOM row's pin_map from a library symbol (fast path) or a CSV (manual).",
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--local-id",
        required=True,
        help="The BOM row's local_id whose pin_map should be (re-)resolved.",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--lib-symbol",
        help="Derive pin_map from a KiCad library symbol's pin-name table, e.g. 'Connector:USB_C_Receptacle'.",
    )
    group.add_argument(
        "--csv",
        help="Path to a CSV file with 'signal,pin' (or 'pin,signal') header and one row per mapping.",
    )
    p.add_argument(
        "--symbols-root",
        default=None,
        help="Override the kicad-symbols library root (defaults to the bundled submodule).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the derived pin_map without writing it back to bom.json.",
    )
    p.set_defaults(func=_cmd_resolve_pin_map)

    p = sub.add_parser("stage4", help="Synthesize nets.json from design_artifact + bom.")
    p.add_argument("--project-dir", required=True)
    p.add_argument("--source", default="det", choices=("det", "llm"), help="Which Stage 0 artifact to use (default: det).")
    p.set_defaults(func=_cmd_stage4)

    p = sub.add_parser("stage5", help="Emit YAML HDM (hdm.yaml) consumable by yaml_to_kicad.py.")
    p.add_argument("--project-dir", required=True)
    p.add_argument("--source", default="det", choices=("det", "llm"))
    p.set_defaults(func=_cmd_stage5)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
