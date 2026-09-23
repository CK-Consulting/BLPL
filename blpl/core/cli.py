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
import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from . import (
    llm_adapter,
    project_manifest,
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
from . import doctor as _doctor
from . import symbol_resolution
from . import init_project as _init


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
# where the kicad-symbols/kicad-footprints submodules live (real submodules).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SYMBOLS = _REPO_ROOT / "kicad-symbols"
_DEFAULT_FOOTPRINTS = _REPO_ROOT / "kicad-footprints"


def _project_dir(args: argparse.Namespace) -> Path:
    p = Path(args.project_dir).resolve()
    (p / ".pipeline").mkdir(parents=True, exist_ok=True)
    return p


def _board(args: argparse.Namespace, proj: Path) -> str | None:
    """Which board this invocation is about, or None for a single-board project.

    None is not a missing value — it is the answer for every project that has
    one board, which is every project written before boards existed. It also
    keeps their artifact names unchanged: ``bom.json`` stays ``bom.json``
    rather than becoming ``bom.<something>.json`` and orphaning what is already
    on disk and in git.
    """
    man = project_manifest.discover(proj)
    requested = getattr(args, "board", None)
    if man.implicit:
        if requested and requested != man.boards[0].name:
            raise SystemExit(
                f"error: {proj.name} is a single-board project; it has no board "
                f"{requested!r}. Add a project.md with a ## Boards section to split it."
            )
        return None
    if not requested:
        names = ", ".join(b.name for b in man.boards)
        raise SystemExit(
            f"error: {proj.name} has more than one board ({names}). "
            "Say which with --board."
        )
    if man.board(requested) is None:
        names = ", ".join(b.name for b in man.boards)
        raise SystemExit(f"error: no board {requested!r} in {proj.name}. Known: {names}")
    return requested


def _artifact(
    args: argparse.Namespace, proj: Path, name: str, *, suffix: str = "json"
) -> Path:
    """Where one stage artifact lives for the board being worked on.

    One ``.pipeline/`` per project, with the board as a filename qualifier — not
    one pipeline per board. Splitting them would fragment ``datasheets/``,
    ``modules/`` and part resolution, so two boards in one project could end up
    holding different absolute maximums for the same part. See
    project_manifest.pipeline_dir for the full argument.
    """
    return project_manifest.artifact_path(proj, name, board=_board(args, proj), suffix=suffix)


def _project_config(args: argparse.Namespace, proj: Path) -> Path:
    """Where this board's project.yaml is: board dir, then project root, then legacy."""
    board = _board(args, proj)
    candidates: list[Path] = []
    if board is not None:
        man = project_manifest.discover(proj)
        candidates.append(project_manifest.board_dir(proj, man, board) / "project.yaml")
    candidates.append(proj / "project.yaml")
    for c in candidates:
        if c.exists():
            return c
    legacy = _artifact(args, proj, "project", suffix="yaml")
    return legacy if legacy.exists() else candidates[0]


def _md_inputs(project_dir: Path, board: str | None = None) -> list[Path]:
    """Return the Markdown input files for a board.

    Default: all *.md files directly under the board's directory. Non-recursive,
    which is what keeps ``.notes/`` correspondence and ``.pipeline/`` output from
    being read as design intent.

    A single-board project's directory *is* the project directory, so this
    behaves exactly as it always did when there is no board.
    """
    root = Path(project_dir)
    if board is not None:
        man = project_manifest.discover(root)
        root = project_manifest.board_dir(root, man, board)
    return [p for p in sorted(root.glob("*.md"))]


def _make_adapter(args: argparse.Namespace) -> llm_adapter.LLMAdapter:
    return llm_adapter.get_adapter(provider=args.llm_provider, model=args.llm_model)


def _cmd_stage0_det(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    md_files = _md_inputs(proj, _board(args, proj))
    if not md_files:
        print(f"error: no .md files found under {proj}", file=sys.stderr)
        return 2
    out = _artifact(args, proj, "design_artifact.deterministic")
    artifact = stage0_deterministic.run(md_files, out)
    print(
        f"stage0-det: {len(artifact['components'])} components, "
        f"{len(artifact['connectors'])} connectors  ({len(md_files)} .md files)"
    )
    print(f"            wrote {out}")
    return 0


def _cmd_stage0_llm(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    md_files = _md_inputs(proj, _board(args, proj))
    if not md_files:
        print(f"error: no .md files found under {proj}", file=sys.stderr)
        return 2
    adapter = _make_adapter(args)
    out = _artifact(args, proj, "design_artifact.llm")
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
    a = _artifact(args, proj, "design_artifact.deterministic")
    b = _artifact(args, proj, "design_artifact.llm")
    if not a.exists() or not b.exists():
        missing = [str(p) for p in (a, b) if not p.exists()]
        print(f"error: missing input artifact(s): {missing}", file=sys.stderr)
        print("       run stage0-det and stage0-llm first", file=sys.stderr)
        return 2
    out = _artifact(args, proj, "design_artifact.compare", suffix="md")
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
    src_path = _artifact(args, proj, f"design_artifact.{source}")
    if source == "det":
        src_path = _artifact(args, proj, "design_artifact.deterministic")
    if not src_path.exists():
        print(f"error: {src_path} does not exist (run stage0-{source} first)", file=sys.stderr)
        return 2
    out = _artifact(args, proj, "bom")
    adapter = _make_adapter(args)
    print(f"stage1: resolving BOM from {src_path.name} via {adapter.provider}/{adapter.model}…", file=sys.stderr)
    bom = stage1_resolve_bom.run(src_path, out, adapter=adapter)
    low = stage1_resolve_bom.low_confidence_rows(bom)
    print(f"stage1: {len(bom['rows'])} rows ({len(low)} low-confidence, need review)")
    print(f"        wrote {out}")
    return 0


def _cmd_stage3(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    coverage = _artifact(args, proj, "coverage_report")
    bom = _artifact(args, proj, "bom")
    for p, label in [(coverage, "coverage_report"), (bom, "bom")]:
        if not p.exists():
            print(f"error: {label} missing at {p}", file=sys.stderr)
            return 2
    result = stage3_generate.run(
        coverage_path=coverage,
        bom_path=bom,
        project_dir=proj,
            board=_board(args, proj),
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
    artifact_path = _artifact(args, proj, f"design_artifact.{args.source}")
    if args.source == "det":
        artifact_path = _artifact(args, proj, "design_artifact.deterministic")
    bom_path = _artifact(args, proj, "bom")
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
    hdm_path = _artifact(args, proj, "hdm", suffix="yaml")
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

    # The emitted board is named for what it *is*. On a multi-board project the
    # project name alone would give two boards the same stem and let the second
    # overwrite the first, so the board joins the name when there is one.
    project_name = proj.name or "project"
    board_name = _board(args, proj)
    stem = f"{project_name}_{board_name}" if board_name else project_name
    base = _re.sub(r"[^A-Za-z0-9._-]+", "_", stem.strip()) or "project"
    out_path = project_manifest.pipeline_dir(proj) / f"{base}_{stamp}.kicad_pcb"

    # The plugin lives inside blpl/; PYTHONPATH needs the parent so
    # `-m blpl.plugin_kicad.build_pcb` resolves under KiCad's Python.
    blpl_parent = _REPO_ROOT
    footprints_root = args.footprints_root or str(_DEFAULT_FOOTPRINTS)

    # KiCad's bundled Python doesn't ship with pyyaml; pre-convert to JSON so
    # the plugin reads via the stdlib json module.
    import json as _json
    import yaml as _yaml

    hdm_json_path = _artifact(args, proj, "hdm.plugin")
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
    """Launch the FastAPI backend locally. Requires the 'webapp' extras.

    This runs *the same* backend the container runs (app/backend), not a second
    one. There used to be two: blpl/webapp served the built React bundle against
    an API that had no auth, no git, and no /design, so the UI's first request
    404'd and nothing worked. `blpl serve` is now a local launcher for the real
    app — the difference between this and docker compose is only where KiCad
    comes from (your PATH here, the pinned image there).

    The container sets its own env in docker-compose.yml. Locally we default the
    same three roots under XDG data, so serving needs no configuration at all.
    """
    try:
        import uvicorn  # type: ignore[import-not-found]
    except ImportError:
        print(
            "error: uvicorn not installed. Install the webapp extras with: "
            "uv pip install -e '.[webapp]'",
            file=sys.stderr,
        )
        return 2

    repo_root = Path(__file__).resolve().parents[2]
    backend_dir = repo_root / "app" / "backend"
    if not (backend_dir / "app" / "main.py").is_file():
        print(f"error: backend not found at {backend_dir}", file=sys.stderr)
        return 2

    data_root = Path(
        os.environ.get("BLPL_DATA_ROOT")
        or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "blpl"
    ).expanduser()

    # BLPL_WORKSPACE is deliberately NOT honoured as a fallback. It meant "a root
    # to scan for projects at any depth", and in practice was pointed straight at
    # a single project directory. BLPL_PROJECTS_ROOT means "the directory whose
    # immediate children are projects" — so silently reusing the old value would
    # scan *inside* one board and report zero projects. Fail loudly instead.
    stale = os.environ.get("BLPL_WORKSPACE")
    if stale and not (args.workspace or os.environ.get("BLPL_PROJECTS_ROOT")):
        print(
            f"blpl serve: ignoring BLPL_WORKSPACE={stale!r} — it is no longer used.\n"
            "  It named one project; the app now lists the children of a projects "
            "directory.\n"
            "  Pass --workspace <dir-containing-your-projects>, or import projects "
            "from the browser.",
            file=sys.stderr,
        )

    projects_root = (
        args.workspace or os.environ.get("BLPL_PROJECTS_ROOT") or data_root / "projects"
    )
    projects_root = Path(projects_root).expanduser().resolve()
    try:
        projects_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: cannot use projects root {projects_root}: {exc}", file=sys.stderr)
        return 2

    os.environ.setdefault("BLPL_DATA_ROOT", str(data_root))
    os.environ["BLPL_PROJECTS_ROOT"] = str(projects_root)
    os.environ.setdefault("BLPL_VAULT_DB", str(data_root / "vault.db"))
    os.environ.setdefault("BLPL_CONFIG", str(data_root / "blpl.toml"))
    os.environ.setdefault("BLPL_KICAD_HAPPY", str(repo_root / "kicad-happy"))
    data_root.mkdir(parents=True, exist_ok=True)

    url = f"http://{args.host}:{args.port}"
    print(f"blpl serve: starting on {url}", file=sys.stderr)
    print(f"blpl serve: projects={projects_root}", file=sys.stderr)
    print(f"blpl serve: data={data_root}", file=sys.stderr)

    if not args.no_browser:
        import webbrowser
        import threading
        import time

        def _open_later() -> None:
            time.sleep(1.0)  # give uvicorn a moment to bind
            webbrowser.open(url)

        threading.Thread(target=_open_later, daemon=True).start()

    # app_dir rather than sys.path: the --reload supervisor spawns a fresh
    # process that would not inherit an in-process sys.path edit.
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        app_dir=str(backend_dir),
    )
    return 0


def _cmd_resolve_pin_map(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    bom = _artifact(args, proj, "bom")
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
    da_path = _artifact(args, proj, f"design_artifact.{args.source}")
    if args.source == "det":
        da_path = _artifact(args, proj, "design_artifact.deterministic")
    bom_path = _artifact(args, proj, "bom")
    if not da_path.exists():
        print(f"error: {da_path} missing — run stage0-{args.source} first", file=sys.stderr)
        return 2
    out = _artifact(args, proj, "nets")
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
    da_path = _artifact(args, proj, "design_artifact.deterministic")
    if args.source == "llm":
        da_path = _artifact(args, proj, "design_artifact.llm")
    bom_path = _artifact(args, proj, "bom")
    nets_path = _artifact(args, proj, "nets")
    # project.yaml is hand-authored (or `blpl init`-generated) config, not a
    # build artifact — so it belongs beside the design markdown where it gets
    # committed, not in .pipeline/ which is generated and gitignored. On a
    # multi-board project each board has its own dimensions and stackup, so
    # the board's own directory is looked in first; the project root is the
    # shared fallback; the legacy .pipeline/ location keeps old projects working.
    proj_cfg = _project_config(args, proj)
    coverage = _artifact(args, proj, "coverage_report")

    for p, label in [(da_path, "design_artifact"), (bom_path, "bom"), (nets_path, "nets")]:
        if not p.exists():
            print(f"error: {label} missing at {p}", file=sys.stderr)
            return 2

    out = _artifact(args, proj, "hdm", suffix="yaml")
    try:
        hdm = stage5_emit_yaml_hdm.run(
            bom_path=bom_path,
            nets_path=nets_path,
            design_artifact_path=da_path,
            project_config_path=proj_cfg,
            output_path=out,
            coverage_path=coverage if coverage.exists() else None,
            project_dir=proj,
            board=_board(args, proj),
            stock_symbols_root=stage6_compile_kicad._DEFAULT_SYMBOLS,
            stock_footprints_root=stage6_compile_kicad._DEFAULT_FOOTPRINTS,
        )
    except stage5_emit_yaml_hdm.MissingProjectConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    comps_all = hdm.get("components", {})
    synthesized = [r for r, c in comps_all.items() if isinstance(c, dict) and c.get("synthesized")]
    tp = (hdm.get("synthesis") or {}).get("test_points") or {}
    print(
        f"stage5: emitted HDM with {len(comps_all) - len(synthesized)} components, "
        f"{len(hdm.get('nets', {}))} nets"
        + (f", {len(synthesized)} test points (policy: {tp.get('policy', '?')})" if synthesized else "")
    )
    print(f"        wrote {out}")

    # A placeholder that slips by unnoticed is how you fab a board with the wrong
    # part on it. Make it impossible to miss at the console, not just in a file.
    comps = hdm.get("components", {})
    report = out.parent / "manual_library_work.md"
    custom = proj / symbol_resolution.CUSTOM_LIB_DIRNAME

    bad_fp = {r: c for r, c in comps.items() if c.get("needs_manual_footprint")}
    if bad_fp:
        print()
        print(f"  ***  {len(bad_fp)} COMPONENT(S) HAVE NO REAL FOOTPRINT  ***")
        print("       A pad-count-correct 2.54mm header was emitted in their place.")
        print("       A wrong footprint is copper. DO NOT FABRICATE THIS BOARD.")
        print()
        for refdes, comp in sorted(bad_fp.items()):
            print(f"       {refdes:<14} wanted {comp.get('requested_footprint')!r} — does not exist")
        print()
        print(f"       Draw them, save into {custom / 'footprints'} as <Lib>.pretty/<Name>.kicad_mod.")

    bad_sym = {r: c for r, c in comps.items() if c.get("needs_manual_symbol")}
    if bad_sym:
        print()
        print(f"  ***  {len(bad_sym)} COMPONENT(S) HAVE NO REAL SYMBOL  ***")
        print("       A generic placeholder was emitted so the board still opens.")
        print("       The placeholder is NOT the part.")
        print()
        for refdes, comp in sorted(bad_sym.items()):
            print(f"       {refdes:<14} wanted {comp.get('requested_symbol')!r} — does not exist")
        print()
        print(f"       Draw them, save into {custom / 'symbols'}.")

    if bad_fp or bad_sym:
        print()
        print(f"       Re-run stage5 once they exist. Details: {report}")
        print()
    return 0


def _cmd_stage2(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    bom_path = _artifact(args, proj, "bom")
    output_path = _artifact(args, proj, "coverage_report")
    if not bom_path.exists():
        print(f"error: no bom.json at {bom_path}", file=sys.stderr)
        return 2
    report = stage2_library_lookup.run(
        bom_path=bom_path,
        symbols_root=Path(args.symbols_root).resolve(),
        footprints_root=Path(args.footprints_root).resolve(),
        # Coverage must see every root Stage 5 resolves against, or a part the
        # shared library holds is re-reported as a gap forever — and worse, the
        # LLM's hint (which coverage exists to outrank) decides instead.
        project_dir=proj,
            board=_board(args, proj),
        output_path=output_path,
    )
    s = report["summary"]
    print(f"stage2: {s['hit']} hit / {s['needs_variant']} needs_variant / {s['miss']} miss (of {s['total']})")
    # The counts alone sent people digging through the report json for which
    # rows they meant — and the json needed a cross-reference to bom.json to
    # say what was even searched. Name them, with what was tried and what
    # (if anything) was found.
    def _side(match: dict | None, query: str) -> str:
        if match is None:
            return f"NO MATCH for '{query}'"
        if match["match_type"] == "exact":
            return "exact"
        score = f" {match['score']:.2f}" if "score" in match else ""
        return f"'{query}' ~ {match['lib']}:{match['name']} ({match['match_type']}{score})"

    for status in ("miss", "needs_variant"):
        rows = [r for r in report["rows"] if r["status"] == status]
        if not rows:
            continue
        print(f"        {status}:")
        for r in rows:
            print(f"          {r['local_id']:<14} symbol: {_side(r['symbol_match'], r['symbol_query'])}")
            print(f"          {'':<14} footprint: {_side(r['footprint_match'], r['footprint_query'])}")
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

    # `--board all` on a multi-board project: every board in manifest order,
    # then the cross-board check, because a project is only built when the
    # boards have been checked against each other. Each board's failure is
    # reported and the rest still run — a broken daughterboard should not hide
    # what the carrier's review has to say.
    if getattr(args, "board", None) == "all":
        proj = _project_dir(args)
        man = project_manifest.discover(proj)
        if man.implicit:
            print(f"error: {proj.name} is a single-board project; omit --board.", file=sys.stderr)
            return 2
        worst = 0
        for b in man.boards:
            print(f"==> board {b.name}", file=sys.stderr)
            try:
                rc = _cmd_run(argparse.Namespace(**{**vars(args), "board": b.name}))
            except Exception:
                # A raised stage is the same event as a stage returning non-zero,
                # and the loop already isolates the second. Without this it did
                # not isolate the first: one board's traceback unwound straight
                # past the remaining boards and the cross-board check, which is
                # the opposite of what the comment above promises.
                traceback.print_exc()
                print(f"==> board {b.name} raised; continuing", file=sys.stderr)
                rc = 1
            worst = max(worst, rc)
        rc = _cmd_crossboard(argparse.Namespace(project_dir=args.project_dir))
        return max(worst, rc)

    # The worst return code any stage produced. --continue-on-error means carry
    # on past a failure, not forget it happened: without this the function ended
    # in an unconditional `return 0`, so a run that continued past a broken
    # stage exited 0 and reported success. Every rc in this function comes
    # through _attempt, so recording it here covers all of them.
    worst_rc = 0

    def _attempt(stage: str, fn, note: str = "") -> int:
        nonlocal worst_rc
        print(f"==> {stage}{' ' + note if note else ''}", file=sys.stderr)
        try:
            rc = fn()
        except Exception:
            # --continue-on-error only ever guarded a non-zero return, so any
            # stage that raised instead of returning ignored the flag entirely
            # and took the whole run with it. A schema validation failure in
            # stage1 is the case that found this. Turning the exception into a
            # non-zero rc lets the existing halt-or-continue logic below decide,
            # which is what the flag is for. KeyboardInterrupt and SystemExit
            # are not Exception subclasses, so Ctrl-C still stops the run.
            traceback.print_exc()
            rc = 1
        worst_rc = max(worst_rc, rc)
        if rc != 0 and not args.continue_on_error:
            print(f"==> {stage} returned {rc}; halting (use --continue-on-error to proceed)", file=sys.stderr)
        return rc

    # Thin namespaces reused for the per-stage command functions.
    base = argparse.Namespace(
        project_dir=args.project_dir,
        llm_provider=args.llm_provider,
        llm_model=args.llm_model,
        # Every stage resolves its own board through `_board`, which reads this.
        # Leaving it out made `run --board base` parse the flag and then drop
        # it, so stage0 saw no board and refused the whole pipeline on any
        # multi-board project.
        board=getattr(args, "board", None),
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
            # Routing sits between emission and validation so Stage 7's DRC and
            # Stage 8's review see the routed board. A missing router is not a
            # failure of the run — the step records why it did not route, and
            # Stage 8 reads that record — so only an attempted-and-failed route
            # is allowed to stop anything.
            if not getattr(args, "no_autoroute", False):
                ns = argparse.Namespace(**vars(base), passes=getattr(args, "passes", 10))
                rc = _attempt("autoroute", lambda: _cmd_autoroute(ns))
                if rc != 0 and not args.continue_on_error:
                    return rc
        elif stage == "stage7":
            rc = _attempt("stage7", lambda: _cmd_stage7(base))
            if rc != 0 and not args.continue_on_error:
                return rc
        elif stage == "stage8":
            ns = argparse.Namespace(
                **vars(base), no_emc=False, no_spice=False,
                lifecycle=getattr(args, "lifecycle", False),
                no_lifecycle=getattr(args, "no_lifecycle", False),
            )
            rc = _attempt("stage8", lambda: _cmd_stage8(ns))
            if rc != 0 and not args.continue_on_error:
                return rc
    return worst_rc


def _cmd_stage6(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    hdm = _artifact(args, proj, "hdm", suffix="yaml")
    if not hdm.exists():
        print(f"error: hdm.yaml missing at {hdm} — run stage5 first", file=sys.stderr)
        return 2
    outputs = stage6_compile_kicad.run(
        hdm, project_manifest.pipeline_dir(proj), project_dir=proj, board=_board(args, proj)
    )
    file_outputs = {k: v for k, v in outputs.items() if k != "base"}
    print(f"stage6: compiled KiCad project ({len(file_outputs)} files, base={outputs['base']})")
    for kind, path in file_outputs.items():
        print(f"        {kind}: {path}")
    return 0


def _newest_emitted(pipeline_dir: Path, suffix: str, board: str | None) -> Path | None:
    """The most recent compile of *this* board.

    Without the board filter a multi-board project validates whichever file
    is newest, so running stage7 on `sensor` right after compiling `base`
    reports on `base` and calls it `sensor` — a clean report for a board
    nobody checked. Stage 8 and the autorouter pick their input the same way,
    for the same reason.
    """
    found = sorted(pipeline_dir.glob(f"*{suffix}"), reverse=True)
    if board is not None:
        found = [p for p in found if f"_{board}_" in p.name]
    return found[0] if found else None


def _cmd_stage7(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    pipeline_dir = project_manifest.pipeline_dir(proj)
    board = _board(args, proj)

    pcb = _newest_emitted(pipeline_dir, ".kicad_pcb", board)
    sch = _newest_emitted(pipeline_dir, ".kicad_sch", board)
    report = stage7_validate.run(
        proj,
        pcb_path=pcb,
        sch_path=sch,
        generated_symbols_dir=proj / "generated" / "symbols",
        coverage_path=_artifact(args, proj, "coverage_report"),
        board=board,
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
    print(f"        wrote {_artifact(args, proj, 'validation_report')}")
    return 0 if report["ok"] else 1


def _cmd_init(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    board = _board(args, proj)
    # A board's project.yaml lives beside that board's markdown: two boards in
    # one project do not share an outline or a stackup, and writing both to the
    # project root would let the second silently describe the first.
    target_dir = None
    if board is not None:
        target_dir = project_manifest.board_dir(proj, project_manifest.discover(proj), board)
    try:
        target, result = _init.write_config(
            proj, force=args.force, board=board, target_dir=target_dir
        )
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"init: wrote {target}")
    for f in result.found:
        print(f"        from your markdown: {f}")
    for d in result.defaulted:
        # Never let a made-up number pass as one the user wrote.
        print(f"        (default, check it): {d}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    report = _doctor.run(proj, board=_board(args, proj))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(_doctor.render_text(report))
    # Errors mean Stage 0 will drop or mangle real design data.
    return 0 if report.ok else 1


def _cmd_skills(args: argparse.Namespace) -> int:
    from . import skills_install

    source = Path(args.source) if args.source else None

    if args.action == "list":
        avail = skills_install.available(source)
        if not avail:
            print(
                "no skills found — is kicad-happy checked out? "
                "(git submodule update --init, or set BLPL_KICAD_HAPPY)",
                file=sys.stderr,
            )
            return 1
        have = skills_install.installed(Path(args.project_dir).resolve()) if args.project_dir else {}
        for name, src in avail.items():
            state = ""
            if name in have:
                state = "  [installed]" if have[name] else "  [present, not blpl-installed]"
            default = "  (default)" if name in skills_install.DEFAULT_SET else ""
            print(f"{name:<18} {src}{default}{state}")
        return 0

    # install
    if not args.project_dir:
        print("error: install needs --project-dir", file=sys.stderr)
        return 2
    proj = Path(args.project_dir).resolve()
    names = args.skill or (
        list(skills_install.available(source)) if getattr(args, "all", False) else skills_install.DEFAULT_SET
    )
    result = skills_install.install(proj, names, source=source, force=args.force)
    for n in result.installed:
        print(f"skills: installed {n} -> {proj / '.claude' / 'skills' / n}")
    for n in result.refreshed:
        print(f"skills: refreshed {n}")
    for n in result.skipped:
        print(f"skills: skipped {n} (already present — use --force to overwrite)")
    for n in result.unknown:
        print(f"skills: unknown skill {n!r} — see `blpl skills list`", file=sys.stderr)
    return 1 if result.unknown else 0


def _cmd_stage8(args: argparse.Namespace) -> int:
    proj = _project_dir(args)
    pipeline_dir = project_manifest.pipeline_dir(proj)
    board = _board(args, proj)
    # Same "most recent timestamped compile of THIS board wins" rule as stage7.
    pcb = _newest_emitted(pipeline_dir, ".kicad_pcb", board)
    sch = _newest_emitted(pipeline_dir, ".kicad_sch", board)
    if pcb is None and sch is None:
        print(
            f"error: no emitted KiCad files in {pipeline_dir} — run stage6 first",
            file=sys.stderr,
        )
        return 2

    lifecycle: bool | None = None
    if getattr(args, "lifecycle", False):
        lifecycle = True
    elif getattr(args, "no_lifecycle", False):
        lifecycle = False

    report = stage8_review.run(
        proj,
        sch_path=sch,
        pcb_path=pcb,
        emc=not args.no_emc,
        spice=not args.no_spice,
        board=board,
        lifecycle=lifecycle,
    )
    if report.get("skipped"):
        print(f"stage8: skipped — {report['reason']}")
        return 0

    s = report["summary"]
    status = "PASS" if report["ok"] else "FAIL"
    print(
        f"stage8: {status}  emitter={s['emitter']}  design={s['design']}  "
        f"expected={s['expected']}  placeholders={s.get('placeholders', 0)}"
    )
    for d in report["emitter_defects"]:
        rule = d.get("rule_id") or d.get("check")
        print(f"        [emitter] {rule}: {d['summary']}")
    if report.get("placeholders"):
        print("        [placeholder] DO NOT FABRICATE — parts of this board are stand-ins:")
        for d in report["placeholders"]:
            print(f"        [placeholder] {d['summary']}")
    routing = report.get("autoroute") or {}
    if routing.get("attempted") and routing.get("ok"):
        left = routing.get("unrouted")
        print(f"        [route] Freerouting ran" + (f", {left} net(s) still open" if left is not None else ""))
    else:
        print(f"        [route] not routed — {routing.get('reason', 'autorouting was not attempted')}")
    lc = report.get("lifecycle") or {}
    if lc.get("ran"):
        print("        [lifecycle] distributor audit ran")
    elif lc.get("reason"):
        print(f"        [lifecycle] not run — {lc['reason']}")
    for rid, why in (report.get("expected_reasons") or {}).items():
        if rid not in ("RT-001", "LC-007"):
            print(f"        [expected] {rid}: {why}")
    sim = report.get("simulation") or {}
    if sim.get("skipped"):
        print(f"        [spice] not run — {sim.get('reason', 'unknown reason')}")
    elif sim.get("nothing_to_simulate") or sim.get("nothing_measured"):
        print(f"        [spice] {sim.get('headline', 'nothing was verified')}")
    elif sim.get("counts"):
        c = sim["counts"]
        print(
            f"        [spice] {c.get('total', 0)} simulated on {sim.get('simulator', '?')} — "
            f"{c.get('pass', 0)} pass, {c.get('warn', 0)} warn, {c.get('fail', 0)} fail"
        )
    print(f"        wrote {_artifact(args, proj, 'review', suffix='md')}")
    # Emitter defects and placeholders gate; design issues are the user's to triage.
    return 0 if report["ok"] else 1


def _cmd_autoroute(args: argparse.Namespace) -> int:
    """Bulk-route the most recent compile of a board and leave a record either way.

    Never fatal to a pipeline run: a missing router is a reason written into
    `.pipeline/autoroute_report[.board].json`, which Stage 8 reads to decide
    whether an unrouted net is a finding about the board or about the tooling.
    Exit 1 only when routing was attempted and failed — that is worth stopping
    for; "no jar installed" is not.
    """
    from . import autoroute

    proj = _project_dir(args)
    pipeline_dir = project_manifest.pipeline_dir(proj)
    board = _board(args, proj)
    pcb = _newest_emitted(pipeline_dir, ".kicad_pcb", board)
    if pcb is None:
        print(f"error: no emitted .kicad_pcb in {pipeline_dir} — run stage6 first", file=sys.stderr)
        return 2
    report_path = _artifact(args, proj, "autoroute_report")
    work = pipeline_dir / ("autoroute" if board is None else f"autoroute.{board}")
    result = autoroute.run_for(pcb, report_path, passes=args.passes, work_dir=work)
    if not result.attempted:
        print(f"autoroute: skipped — {result.reason}")
        print(f"           wrote {report_path}")
        return 0
    if not result.ok:
        print(f"autoroute: FAILED — {result.reason}", file=sys.stderr)
        print(f"           wrote {report_path}", file=sys.stderr)
        return 1
    left = f", {result.unrouted} net(s) still open" if result.unrouted is not None else ""
    print(f"autoroute: routed {pcb.name} in {result.passes} passes{left}")
    print(f"           snapshot {result.snapshot}")
    print(f"           wrote {report_path}")
    return 0


def _cmd_crossboard(args: argparse.Namespace) -> int:
    """Check every declared mate in every configuration, across the project.

    Project-level, not per-board: the report is about what happens where two
    boards meet, which no board's own pipeline can see. Reads each board's
    Stage 0 artifact, so boards that have not run Stage 0 are reported as
    unbuilt rather than silently left out of the check.
    """
    from . import crossboard

    proj = _project_dir(args)
    try:
        man = project_manifest.discover(proj)
    except project_manifest.ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if man.implicit:
        print(f"crossboard: {proj.name} is a single-board project; nothing mates with anything.")
        return 0
    for w in man.warnings:
        print(f"crossboard: project.md: {w}", file=sys.stderr)

    artifacts: dict[str, dict] = {}
    for b in man.boards:
        art = project_manifest.artifact_path(proj, "design_artifact.deterministic", board=b.name)
        if art.is_file():
            try:
                artifacts[b.name] = json.loads(art.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    report = crossboard.check(man, artifacts)
    out = project_manifest.artifact_path(proj, "crossboard")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")

    by_sev: dict[str, int] = {}
    for f in report.findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    status = "BLOCKED" if report.blocked else "PASS"
    print(
        f"crossboard: {status}  configurations={', '.join(report.checked)}  "
        f"errors={by_sev.get('error', 0)}  warnings={by_sev.get('warning', 0)}"
    )
    for f in report.findings:
        where = f" [{f.mate}{' pin ' + f.pin if f.pin else ''}]" if f.mate else ""
        print(f"        [{f.severity}] {f.configuration}: {f.kind}{where} — {f.message}")
    print(f"        wrote {out}")
    return 1 if report.blocked else 0


def _cmd_spice(args: argparse.Namespace) -> int:
    """Re-simulate without re-running the analyzers.

    Stage 8 already simulates once. This exists because the *interesting* runs
    are the second and third — narrowing to one subcircuit type, or asking for
    a Monte Carlo sweep — and paying for a full schematic and PCB analysis to
    change one flag is the kind of friction that stops people looking.
    """
    from ..agent.tools.spice import find_simulator, simulate

    proj = _project_dir(args)
    _rb = _board(args, proj)
    review_dir = project_manifest.pipeline_dir(proj) / ("review" if _rb is None else f"review.{_rb}")
    schematic_json = review_dir / "schematic.json"
    if not schematic_json.is_file():
        print(
            f"error: no schematic analysis at {schematic_json} — run stage8 first",
            file=sys.stderr,
        )
        return 2

    status = find_simulator(args.simulator)
    if not status.available:
        print(f"spice: skipped — {status.detail}", file=sys.stderr)
        return 2

    pcb_json = review_dir / "pcb.json"
    run = simulate(
        schematic_json,
        review_dir / "spice.json",
        pcb_json=pcb_json if pcb_json.is_file() else None,
        types=[t.strip() for t in args.types.split(",")] if args.types else None,
        timeout=args.timeout,
        monte_carlo=args.monte_carlo,
        simulator=args.simulator,
    )
    if not run.ok:
        print(f"spice: {run.reason}", file=sys.stderr)
        return 1

    print(f"spice: {run.headline()}")
    for r in run.results:
        if r.status == "pass":
            continue
        detail = ", ".join(f"{k} {v:+.1f}%" for k, v in (r.delta or {}).items() if v is not None)
        # A skip's reason is the whole story — "skip" alone tells the user
        # nothing about whether their circuit or our tooling fell short.
        detail = detail or r.note
        print(
            f"        [{r.status}] {r.subcircuit_type} {r.reference}"
            + (f" — {detail}" if detail else "")
        )
    print(f"        wrote {run.report_json}")
    # A failing simulation is a finding about the design, not a broken run —
    # same reason stage8 does not gate on design issues.
    return 0


def _cmd_bom_check(args: argparse.Namespace) -> int:
    """What the emitted board is still missing before anyone can order it.

    Deliberately reads the emitted `.kicad_sch` rather than `bom.json`: the
    point is to catch fields the emitter dropped on the way out, which no
    artifact-to-artifact comparison can see.
    """
    from ..agent.tools.bom import sourcing_gaps

    proj = _project_dir(args)
    pipeline = proj / ".pipeline"
    candidates = sorted(pipeline.glob("*.kicad_sch"), reverse=True) or sorted(
        proj.glob("*.kicad_sch"), reverse=True
    )
    if not candidates:
        print(f"error: no .kicad_sch in {proj} — run stage6 first", file=sys.stderr)
        return 2

    report = sourcing_gaps(candidates[0])
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    if report.get("skipped"):
        print(f"bom-check: skipped — {report['reason']}")
        return 0
    if not report.get("ok"):
        print(f"bom-check: failed — {report.get('reason', 'unknown')}", file=sys.stderr)
        return 1

    total = report.get("total_components") or report.get("component_count") or 0
    gaps = report.get("gaps") or report.get("missing") or []
    print(f"bom-check: {total} components, {len(gaps)} with sourcing gaps")
    for gap in gaps[:20]:
        if isinstance(gap, dict):
            ref = gap.get("reference") or gap.get("refdes") or "?"
            missing = ", ".join(gap.get("missing_fields") or []) or "incomplete"
            print(f"        {ref}: {missing}")
    if len(gaps) > 20:
        print(f"        … and {len(gaps) - 20} more")
    # Gaps are the user's to fill, not a pipeline failure.
    return 0


def _cmd_bom_assembly(args: argparse.Namespace) -> int:
    """Write the per-house upload files from the newest release package."""
    from ..agent.tools.bom import HOUSES, build_assembly
    from .stage6_compile_kicad import _DEFAULT_FOOTPRINTS

    proj = _project_dir(args)
    release_root = proj / "release"
    stamps = sorted((d for d in release_root.glob("*") if d.is_dir()), reverse=True)
    if not stamps:
        print(
            f"error: no release package in {release_root} — run the release build first",
            file=sys.stderr,
        )
        return 2
    out_dir = stamps[0]
    boms = sorted(out_dir.glob("bom/*-bom.csv"))
    if not boms:
        print(f"error: no BOM CSV in {out_dir / 'bom'}", file=sys.stderr)
        return 2

    positions = out_dir / "placement" / "positions.csv"
    houses = HOUSES if args.house == "both" else (args.house,)
    rc = 0
    for house in houses:
        pkg = build_assembly(
            boms[0],
            positions if positions.is_file() else None,
            out_dir / "assembly",
            house=house,
            footprint_roots=[_DEFAULT_FOOTPRINTS],
            lcsc=args.lcsc,
        )
        if not pkg.ok:
            print(f"{house}: failed — {pkg.reason}", file=sys.stderr)
            rc = 1
            continue
        print(f"{house}: {len(pkg.files)} file(s)")
        for f in pkg.files:
            print(f"        {f.name}: {f.rows} rows → {f.path}")
        for w in pkg.warnings:
            print(f"        ! {w}")
    return rc


def _add_lifecycle_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--lifecycle",
        action="store_true",
        help=(
            "Force the distributor lifecycle audit in stage8, even with no keys "
            "(LCSC needs none). Default: run it when distributor credentials are set."
        ),
    )
    g.add_argument(
        "--no-lifecycle",
        action="store_true",
        help="Skip the distributor lifecycle audit in stage8.",
    )


def _add_llm_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--llm-provider", default=None, help="anthropic | openai | ollama (falls back to HDM_LLM_PROVIDER env)")
    p.add_argument("--llm-model", default=None, help="provider-specific model ID (falls back to HDM_LLM_MODEL env)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blpl")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stage0-det", help="Deterministic Markdown -> design_artifact.json.")
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.set_defaults(func=_cmd_stage0_det)

    p = sub.add_parser("stage0-llm", help="LLM Markdown -> design_artifact.json.")
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    _add_llm_flags(p)
    p.set_defaults(func=_cmd_stage0_llm)

    p = sub.add_parser("stage0-compare", help="Diff deterministic vs LLM design_artifact outputs.")
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.set_defaults(func=_cmd_stage0_compare)

    p = sub.add_parser("stage1", help="LLM design_artifact -> bom.json.")
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
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
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.add_argument(
        "--source",
        default="det",
        choices=("det", "llm"),
        help="Which Stage 0 artifact to read connectors from (default: deterministic).",
    )
    p.set_defaults(func=_cmd_stage1_synthesize_connectors)

    p = sub.add_parser("stage2", help="Scan KiCad libraries for symbol/footprint coverage.")
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.add_argument("--symbols-root", default=str(_DEFAULT_SYMBOLS))
    p.add_argument("--footprints-root", default=str(_DEFAULT_FOOTPRINTS))
    p.set_defaults(func=_cmd_stage2)

    p = sub.add_parser("stage6", help="Compile hdm.yaml to .kicad_pcb + .kicad_pro via yaml_to_kicad.py.")
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.set_defaults(func=_cmd_stage6)

    p = sub.add_parser(
        "stage6-plugin",
        help="Build .kicad_pcb via the KiCad plugin (pcbnew Python API). Requires KiCad installed.",
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
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
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.set_defaults(func=_cmd_stage7)

    p = sub.add_parser(
        "init",
        help="Generate project.yaml from the design markdown (identity/stackup/net-class tables).",
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.add_argument("--force", action="store_true", help="Overwrite an existing project.yaml.")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser(
        "doctor",
        help="Preflight: report what Stage 0 would silently discard. Run this first.",
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    p.set_defaults(func=_cmd_doctor)

    p = sub.add_parser(
        "stage8",
        help="Design review (kicad-happy): schematic + PCB + EMC analysis, write review_report.json.",
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.add_argument(
        "--no-emc",
        action="store_true",
        help="Skip the EMC rule pass (the slowest analyzer).",
    )
    p.add_argument(
        "--no-spice",
        action="store_true",
        help="Skip SPICE simulation of the detected subcircuits.",
    )
    _add_lifecycle_flags(p)
    p.set_defaults(func=_cmd_stage8)

    p = sub.add_parser(
        "autoroute",
        help=(
            "Bulk-route the latest compiled board with Freerouting (needs java and "
            "FREEROUTING_JAR); always writes autoroute_report.json saying what happened."
        ),
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.add_argument("--passes", type=int, default=10, help="Freerouting optimisation passes (default 10).")
    p.set_defaults(func=_cmd_autoroute)

    p = sub.add_parser(
        "crossboard",
        help="Check every declared mate in project.md pin by pin; writes .pipeline/crossboard.json.",
    )
    p.add_argument("--project-dir", required=True)
    p.set_defaults(func=_cmd_crossboard)

    p = sub.add_parser(
        "spice",
        help="Simulate the subcircuits stage8 detected (needs ngspice/LTspice/Xyce).",
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.add_argument(
        "--types",
        help="Comma-separated subcircuit types to simulate (default: all supported).",
    )
    p.add_argument(
        "--timeout", type=int, default=5, help="Seconds per simulation (default: 5)."
    )
    p.add_argument(
        "--monte-carlo",
        type=int,
        default=0,
        metavar="N",
        help="Run N tolerance samples per subcircuit, to see whether it passes on real parts.",
    )
    p.add_argument(
        "--simulator",
        default="auto",
        choices=("auto", "ngspice", "ltspice", "xyce"),
        help="Which simulator to use (default: auto-detect).",
    )
    p.set_defaults(func=_cmd_spice)

    p = sub.add_parser(
        "bom-check",
        help="Sourcing readiness of the emitted schematic: which parts cannot be ordered yet.",
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.add_argument("--json", action="store_true", help="Emit the analyzer report as JSON.")
    p.set_defaults(func=_cmd_bom_check)

    p = sub.add_parser(
        "bom-assembly",
        help="Write JLCPCB / PCBWay upload files (BOM + CPL) from the latest release package.",
    )
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.add_argument(
        "--house",
        default="both",
        choices=("both", "jlcpcb", "pcbway"),
        help="Which assembly house to write files for (default: both).",
    )
    p.add_argument(
        "--lcsc",
        action="store_true",
        help="Look up LCSC part numbers for the JLCPCB BOM. Needs network; JLCPCB "
             "orders assembly by LCSC number, so without this the BOM is not buildable.",
    )
    p.set_defaults(func=_cmd_bom_assembly)

    p = sub.add_parser("run", help="Run the full pipeline end-to-end (stages 0–8).")
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several. "
            "'all' runs every board in turn and then the cross-board check."
        ),
    )
    p.add_argument(
        "--no-autoroute",
        action="store_true",
        help="Skip the Freerouting pass that otherwise runs after stage6 when a router is available.",
    )
    p.add_argument("--passes", type=int, default=10, help="Freerouting optimisation passes (default 10).")
    _add_lifecycle_flags(p)
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
        help=(
            "Directory holding your BLPL projects (one subdirectory each). "
            "Defaults to $BLPL_PROJECTS_ROOT, else <data-root>/projects. "
            "(The old $BLPL_WORKSPACE named a single project and is no longer used.)"
        ),
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

    p = sub.add_parser(
        "skills",
        help="Install BLPL's Claude Code skills (hardware-design + the kicad-happy set) into a project's .claude/skills/.",
    )
    p.add_argument("action", choices=("list", "install"))
    p.add_argument("--project-dir", help="Required for install; optional for list (adds installed state).")
    p.add_argument(
        "--skill", action="append",
        help="Skill to install, repeatable. Default: the review set "
        "(hardware-design, kicad, emc, bom, datasheets, spice).",
    )
    p.add_argument("--all", action="store_true", help="Install every available skill, sourcing/fab included.")
    p.add_argument("--force", action="store_true", help="Overwrite skills already present in the project.")
    p.add_argument("--source", help="Explicit kicad-happy checkout (overrides BLPL_KICAD_HAPPY and the submodule).")
    p.set_defaults(func=_cmd_skills)

    p = sub.add_parser("stage3", help="Emit gap prompts (and optionally auto-generate generic symbols).")
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
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
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
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
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.add_argument("--source", default="det", choices=("det", "llm"), help="Which Stage 0 artifact to use (default: det).")
    p.set_defaults(func=_cmd_stage4)

    p = sub.add_parser("stage5", help="Emit YAML HDM (hdm.yaml) consumable by yaml_to_kicad.py.")
    p.add_argument("--project-dir", required=True)
    p.add_argument(
        "--board",
        help=(
            "Which board, for a project that has more than one. Omit it on a "
            "single-board project; required when project.md declares several."
        ),
    )
    p.add_argument("--source", default="det", choices=("det", "llm"))
    p.set_defaults(func=_cmd_stage5)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
