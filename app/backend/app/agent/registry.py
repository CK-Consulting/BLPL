"""The tools a design conversation can call, and what each is allowed to do.

Grouped by what they touch, because that is what decides their policy:

  *project* — read design documents and pipeline artifacts, propose an edit.
              Reads are automatic; the "write" is a proposal a human accepts,
              so it needs no approval of its own.
  *parts*   — reach distributor APIs. Network egress with your API keys on it,
              so it asks once per session.
  *depth*   — datasheet download and extraction. These spend real money per
              call and write into the project, so they ask every time.

The descriptions are written for the model and say *when* to reach for a tool,
not just what it does — a tool description that only states its function gets
called at the wrong moments, and this set has expensive members.
"""

from __future__ import annotations

import json
from pathlib import Path

from blpl.agent.tools.bom import HOUSES as HOUSES_FOR_SCHEMA
from blpl.agent.tools.parts import fetch_datasheet, search_parts

from .toolspec import ToolContext, ToolDenied, ToolSpec

# Design documents, matching the editor's rule so the assistant can never
# propose a file the user has no way to open.
EDITABLE_SUFFIXES = {".md", ".markdown", ".yaml", ".yml"}


def _project_file(ctx: ToolContext, name: str) -> Path:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ToolDenied(f"{name!r} must be a bare filename in the project root")
    target = (ctx.project_dir / name).resolve()
    if target.parent != ctx.project_dir.resolve():
        raise ToolDenied(f"{name!r} must be a bare filename in the project root")
    if target.suffix.lower() not in EDITABLE_SUFFIXES:
        raise ToolDenied(
            f"{name!r} is not an editable design document ({', '.join(sorted(EDITABLE_SUFFIXES))})"
        )
    return target


# ---------------------------------------------------------------------------
# Project tools
# ---------------------------------------------------------------------------


async def _list_files(ctx: ToolContext, args: dict) -> str:
    docs = sorted(
        f.name
        for f in ctx.project_dir.iterdir()
        if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in EDITABLE_SUFFIXES
    )
    pipeline = ctx.project_dir / ".pipeline"
    arts = sorted(f.name for f in pipeline.iterdir() if f.is_file()) if pipeline.is_dir() else []
    sheets = ctx.project_dir / "datasheets"
    pdfs = sorted(f.name for f in sheets.glob("*.pdf")) if sheets.is_dir() else []
    return json.dumps(
        {"design_documents": docs, "pipeline_artifacts": arts, "datasheets": pdfs}, indent=2
    )


async def _read_file(ctx: ToolContext, args: dict) -> str:
    from ..chat import sha_of  # local import: chat owns the proposal hashing

    target = _project_file(ctx, str(args.get("path", "")))
    ctx.sandbox.check_read(target)
    if not target.is_file():
        raise FileNotFoundError(f"no file {target.name!r} in this project")
    text = target.read_text(encoding="utf-8", errors="replace")
    ctx.read_shas[target.name] = sha_of(text)
    return text


async def _read_artifact(ctx: ToolContext, args: dict) -> str:
    name = str(args.get("name", ""))
    if not name or "/" in name or "\\" in name:
        raise ToolDenied("artifact name must be a bare filename inside .pipeline/")
    pipeline = (ctx.project_dir / ".pipeline").resolve()
    target = (pipeline / name).resolve()
    if not target.is_relative_to(pipeline):
        raise ToolDenied("artifact name must be a bare filename inside .pipeline/")
    ctx.sandbox.check_read(target)
    if not target.is_file():
        raise FileNotFoundError(f"no artifact {name!r} — has that stage run yet?")
    return target.read_text(encoding="utf-8", errors="replace")


async def _propose_edit(ctx: ToolContext, args: dict) -> str:
    from ..chat import ProposalStore, sha_of

    target = _project_file(ctx, str(args.get("path", "")))
    new_content = args.get("new_content")
    if not isinstance(new_content, str):
        raise ToolDenied("new_content must be the complete new file content, as a string")
    ctx.sandbox.check_write(target)

    exists = target.is_file()
    current = target.read_text(encoding="utf-8", errors="replace") if exists else None
    if current is not None and sha_of(current) == sha_of(new_content):
        raise ToolDenied(f"{target.name} already has exactly this content — nothing to propose")

    base_sha = ctx.read_shas.get(target.name) or (sha_of(current) if current is not None else None)
    proposal = ProposalStore(ctx.project_dir).create(
        path=target.name,
        new_content=new_content,
        rationale=str(args.get("rationale", "")),
        base_sha=base_sha,
        conversation=ctx.conversation,
    )
    return (
        f"Proposed to {'update' if exists else 'create'} {target.name} (proposal {proposal.id}). "
        "The user must accept it before anything is written; do not assume it is applied."
    )


# ---------------------------------------------------------------------------
# Parts tools
# ---------------------------------------------------------------------------


async def _search_parts(ctx: ToolContext, args: dict) -> str:
    mpn = str(args.get("mpn", "")).strip()
    if not mpn:
        raise ToolDenied("mpn is required")
    ctx.note(f"looking up {mpn}")
    result = await _to_thread(search_parts, mpn, creds=ctx.creds)
    return json.dumps(result.to_dict(), indent=2, ensure_ascii=False)


async def _fetch_datasheet(ctx: ToolContext, args: dict) -> str:
    mpn = str(args.get("mpn", "")).strip()
    if not mpn:
        raise ToolDenied("mpn is required")
    dest = ctx.project_dir / "datasheets"
    ctx.sandbox.check_write(dest)
    ctx.note(f"downloading datasheet for {mpn}")
    result = await _to_thread(fetch_datasheet, mpn, dest, creds=ctx.creds)
    return json.dumps(result.to_dict(), indent=2)


async def _extract_datasheet(ctx: ToolContext, args: dict) -> str:
    from blpl.agent.tools.datasheets import extract_datasheet

    mpn = str(args.get("mpn", "")).strip()
    if not mpn:
        raise ToolDenied("mpn is required")
    endpoint = ctx.endpoint_for("datasheet_vision")
    if endpoint is None:
        raise ToolDenied(
            "no vision-capable endpoint is routed to datasheet_vision — set one in Settings. "
            "Extraction reads PDF pages as images; a text-only model would read nothing."
        )
    from blpl.agent.tools import datasheet_files

    # A vendor almost never names a PDF after the orderable part number, so
    # `<MPN>.pdf` only ever worked for files this tool downloaded itself.
    found = datasheet_files.resolve(ctx.project_dir, mpn, file=str(args.get("file") or ""))
    if not found.ok:
        raise FileNotFoundError(
            f"no datasheet resolved for {mpn}: {found.detail}. "
            + (
                f"Files present: {', '.join(found.candidates)}. "
                if found.candidates
                else ""
            )
            + "Pass `file` to name one directly, add a row to "
            f"datasheets/{datasheet_files.MAP_NAME}, or call fetch_datasheet first."
        )
    pdf = found.path
    if found.how in ("explicit", "prefix"):
        # Remember what was worked out, so the next run is a lookup rather than
        # another guess — and so a person can see and correct the binding.
        try:
            datasheet_files.record(ctx.project_dir, mpn, pdf.name)
        except OSError:
            pass
    if found.how != "exact":
        ctx.note(f"{mpn}: reading {pdf.name} (matched by {found.how})")
    cache = ctx.project_dir / "datasheets" / "extracted"
    ctx.sandbox.check_write(cache)
    run = await extract_datasheet(mpn, pdf, cache, endpoint, on_progress=ctx.note)
    return json.dumps(run.to_dict(), indent=2)


async def _read_extraction(ctx: ToolContext, args: dict) -> str:
    mpn = str(args.get("mpn", "")).strip()
    path = (ctx.project_dir / "datasheets" / "extracted" / f"{mpn}.json").resolve()
    ctx.sandbox.check_read(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"no extracted specs for {mpn} — run extract_datasheet_specs first"
        )
    return path.read_text(encoding="utf-8", errors="replace")


async def _to_thread(fn, *a, **kw):
    """Distributor calls are blocking HTTP in a subprocess; keep the loop free."""
    import asyncio

    return await asyncio.to_thread(fn, *a, **kw)


# ---------------------------------------------------------------------------
# Import and module extraction
# ---------------------------------------------------------------------------


async def _read_board(ctx: ToolContext, args: dict) -> str:
    from blpl.importer_kicad import read_project

    sub = str(args.get("directory", "")).strip() or "."
    target = (ctx.project_dir / sub).resolve()
    if not target.is_relative_to(ctx.project_dir.resolve()):
        raise ToolDenied("directory must be inside the project")
    ctx.sandbox.check_read(target)
    if not target.is_dir():
        raise FileNotFoundError(f"no directory {sub!r} in this project")
    ctx.note(f"reading KiCad design under {sub}")
    board = await _to_thread(read_project, target)
    ctx.extras["imported_board"] = board
    data = board.to_dict()
    # The full net list of a real board is enormous and rarely what the next
    # question needs; the summary plus components is what a human would skim.
    data["nets"] = data["nets"][:40]
    if len(board.nets) > 40:
        data["nets_note"] = f"showing 40 of {len(board.nets)} nets — ask about specific ones"
    return json.dumps(data, indent=2)


async def _plan_module(ctx: ToolContext, args: dict) -> str:
    from blpl.importer_kicad import plan_module, read_project

    refdes = [str(r) for r in (args.get("refdes") or [])]
    if not refdes:
        raise ToolDenied("refdes is required — name the parts that make up the block")
    board = ctx.extras.get("imported_board")
    if board is None:
        sub = str(args.get("directory", "")).strip() or "."
        board = await _to_thread(read_project, (ctx.project_dir / sub).resolve())
        ctx.extras["imported_board"] = board

    spec = plan_module(
        board,
        refdes,
        name=str(args.get("name") or "module"),
        description=str(args.get("description", "")),
    )
    ctx.extras["module_spec"] = spec
    return json.dumps(spec.to_manifest(), indent=2)


async def _extract_module(ctx: ToolContext, args: dict) -> str:
    from blpl.core.symbol_resolution import search_path
    from blpl.core.stage6_compile_kicad import _DEFAULT_FOOTPRINTS, _DEFAULT_SYMBOLS
    from blpl.importer_kicad import plan_module, read_project, write_module

    name = str(args.get("name", "")).strip()
    if not name or "/" in name or name.startswith("."):
        raise ToolDenied("name must be a simple module name")
    refdes = [str(r) for r in (args.get("refdes") or [])]
    board = ctx.extras.get("imported_board")
    if board is None:
        board = await _to_thread(read_project, ctx.project_dir)
    spec = plan_module(
        board, refdes, name=name, description=str(args.get("description", ""))
    )
    if not spec.components:
        raise ToolDenied("none of those parts are on this board — nothing to extract")

    dest_root = ctx.project_dir
    ctx.sandbox.check_write(dest_root / "modules")
    symbol_roots = [p for p, _ in search_path(ctx.project_dir, _DEFAULT_SYMBOLS)]
    path, notes = await _to_thread(
        write_module,
        spec,
        dest_root,
        symbol_roots=symbol_roots,
        footprint_root=_DEFAULT_FOOTPRINTS,
        overwrite=bool(args.get("overwrite")),
    )
    ctx.note(f"wrote module {name}")
    return json.dumps(
        {"module": name, "path": str(path), "ports": len(spec.ports),
         "components": len(spec.components), "notes": notes + spec.warnings},
        indent=2,
    )


async def _list_modules(ctx: ToolContext, args: dict) -> str:
    from blpl.core.symbol_resolution import shared_modules_root
    from blpl.importer_kicad import list_modules

    found = await _to_thread(list_modules, ctx.project_dir, shared_modules_root())
    return json.dumps({"modules": found}, indent=2)


def module_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="read_kicad_design",
            description=(
                "Read an existing KiCad design in this project — schematics and PCB — into "
                "components and nets. Use this when the project contains a board somebody else "
                "made (an imported open-hardware design, a previous revision) and you need to "
                "know what is actually on it before answering."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Subdirectory to read; omit for the project root.",
                    }
                },
            },
            kind="file_read",
            handler=_read_board,
        ),
        ToolSpec(
            name="plan_module_extraction",
            description=(
                "Work out what extracting a set of parts as a reusable module would produce — "
                "above all its interface: the nets that cross the boundary and become the module's "
                "ports. Always do this before extracting, and walk the port list with the user: "
                "the ports are the contract a future carrier board has to satisfy, and a wrong "
                "boundary is much cheaper to fix here than after the module exists."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "refdes": {"type": "array", "items": {"type": "string"}},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "directory": {"type": "string"},
                },
                "required": ["refdes", "name"],
            },
            kind="file_read",
            handler=_plan_module,
        ),
        ToolSpec(
            name="extract_module",
            description=(
                "Write a reusable module directory from a set of parts: its manifest and interface, "
                "the symbols and footprints it uses (copied in, so it survives the source board "
                "disappearing), and its BOM. Run plan_module_extraction first and get the user to "
                "agree with the ports."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "refdes": {"type": "array", "items": {"type": "string"}},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["refdes", "name"],
            },
            kind="file_mutation",
            handler=_extract_module,
            approval="ask_always",
        ),
        ToolSpec(
            name="list_modules",
            description=(
                "List reusable modules available to this project — its own, and the shared "
                "library. Check here before designing a block from scratch: a proven one may "
                "already exist."
            ),
            input_schema={"type": "object", "properties": {}},
            kind="query",
            handler=_list_modules,
        ),
    ]


# ---------------------------------------------------------------------------
# Getting to a quote
# ---------------------------------------------------------------------------


async def _fab_readiness(ctx: ToolContext, args: dict) -> str:
    from blpl.core.release import run_gate

    ctx.note("checking fabrication readiness")
    gate = await _to_thread(run_gate, ctx.project_dir / ".pipeline", ctx.project_dir / ".pipeline")
    return json.dumps(gate, indent=2)[:20000]


async def _simulate(ctx: ToolContext, args: dict) -> str:
    from blpl.agent.tools.spice import simulate

    review = ctx.project_dir / ".pipeline" / "review"
    schematic_json = review / "schematic.json"
    if not schematic_json.is_file():
        raise FileNotFoundError(
            "no schematic analysis to simulate — run stage8 first, which is also "
            "what detects which subcircuits are simulatable"
        )
    ctx.sandbox.check_write(review)
    pcb_json = review / "pcb.json"
    mc = int(args.get("monte_carlo") or 0)
    ctx.note("simulating subcircuits" + (f" ({mc} tolerance samples each)" if mc else ""))
    run = await _to_thread(
        simulate,
        schematic_json,
        review / "spice.json",
        pcb_json=pcb_json if pcb_json.is_file() else None,
        types=[str(t) for t in (args.get("types") or [])] or None,
        monte_carlo=mc,
    )
    return json.dumps(run.to_dict(), indent=2)[:20000]


async def _sourcing_gaps(ctx: ToolContext, args: dict) -> str:
    from blpl.agent.tools.bom import sourcing_gaps

    pipeline = ctx.project_dir / ".pipeline"
    scans = sorted(pipeline.glob("*.kicad_sch"), reverse=True) or sorted(
        ctx.project_dir.glob("*.kicad_sch"), reverse=True
    )
    if not scans:
        raise FileNotFoundError("this project has no emitted schematic yet — run stage6 first")
    ctx.sandbox.check_read(scans[0])
    ctx.note("checking what still blocks an order")
    return json.dumps(await _to_thread(sourcing_gaps, scans[0]), indent=2)[:20000]


async def _assembly_files(ctx: ToolContext, args: dict) -> str:
    from blpl.agent.tools.bom import HOUSES, build_assembly
    from blpl.core.stage6_compile_kicad import _DEFAULT_FOOTPRINTS

    house = str(args.get("house") or "jlcpcb").lower()
    if house not in HOUSES:
        raise ToolDenied(f"house must be one of {', '.join(HOUSES)}")
    release_root = ctx.project_dir / "release"
    stamps = sorted((d for d in release_root.glob("*") if d.is_dir()), reverse=True)
    if not stamps:
        raise FileNotFoundError(
            "no release package yet — build the release first; the assembly files are "
            "translations of what it exports"
        )
    out_dir = stamps[0]
    boms = sorted(out_dir.glob("bom/*-bom.csv"))
    if not boms:
        raise FileNotFoundError("the release package has no BOM CSV to translate")

    ctx.sandbox.check_write(out_dir / "assembly")
    lcsc = bool(args.get("lcsc"))
    ctx.note(f"writing {house} upload files" + (" with LCSC lookup" if lcsc else ""))
    positions = out_dir / "placement" / "positions.csv"
    pkg = await _to_thread(
        build_assembly,
        boms[0],
        positions if positions.is_file() else None,
        out_dir / "assembly",
        house=house,
        footprint_roots=[_DEFAULT_FOOTPRINTS],
        lcsc=lcsc,
        creds=ctx.creds,
    )
    return json.dumps(pkg.to_dict(), indent=2)[:20000]


async def _bulk_route(ctx: ToolContext, args: dict) -> str:
    from blpl.core import autoroute

    pcbs = sorted(ctx.project_dir.glob("*.kicad_pcb"))
    if not pcbs:
        raise ToolDenied("this project has no .kicad_pcb yet — run stage6 first")
    ok, why = await _to_thread(autoroute.available)
    if not ok:
        raise ToolDenied(why)

    # Same anchor the interactive KiCad edits use: an autoroute is the single
    # largest change anything can make to a board, and "undo it" has to mean
    # something.
    from .kicad_bridge import snapshot

    if not ctx.extras.get("kicad_snapshot"):
        commit = await _to_thread(snapshot, ctx.project_dir)
        ctx.extras["kicad_snapshot"] = commit or "none"
        ctx.note("snapshotted the board before routing" if commit else "no git snapshot available")

    ctx.sandbox.check_write(pcbs[0])
    ctx.note("bulk routing — this can take several minutes")
    result = await _to_thread(autoroute.route, pcbs[0], passes=int(args.get("passes") or 10))
    return json.dumps(
        {
            **result.to_dict(),
            "next": (
                "run stage7 to DRC the result — an autorouter optimises for completing "
                "connections, not for a board that works"
            ),
        },
        indent=2,
    )


def fab_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="check_fab_readiness",
            description=(
                "Run the fabrication release gate over the latest analysis and report what still "
                "blocks a quote. Use this when the user asks whether the board is ready, or before "
                "suggesting they send anything to a board house — a BLPL board opens and renders "
                "perfectly while every net is still unrouted, so looking finished means nothing."
            ),
            input_schema={"type": "object", "properties": {}},
            kind="query",
            handler=_fab_readiness,
        ),
        ToolSpec(
            name="check_sourcing_gaps",
            description=(
                "Read the emitted schematic and report which parts cannot be ordered yet — "
                "missing manufacturer part numbers, missing distributor part numbers, "
                "inconsistent part-number conventions. Use this before anyone starts a BOM "
                "or asks what a build would cost: a board can pass every electrical check "
                "and still be unorderable, and that is invisible until someone tries."
            ),
            input_schema={"type": "object", "properties": {}},
            kind="file_read",
            handler=_sourcing_gaps,
        ),
        ToolSpec(
            name="write_assembly_files",
            description=(
                "Translate the release package's BOM and placement file into one assembly "
                "house's upload format. The two houses are not interchangeable: JLCPCB "
                "orders by LCSC part number, PCBWay sources turnkey by MPN, and they take "
                "different columns. Pass lcsc=true for JLCPCB — without the LCSC numbers "
                "the BOM uploads and then cannot be built, which is the expensive way to "
                "find out. Requires a release package to already exist."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "house": {
                        "type": "string",
                        "enum": list(HOUSES_FOR_SCHEMA),
                        "description": "Which assembly house the files are for.",
                    },
                    "lcsc": {
                        "type": "boolean",
                        "description": "Look up LCSC part numbers (network). JLCPCB needs them.",
                    },
                },
                "required": ["house"],
            },
            kind="network",
            handler=_assembly_files,
            approval="ask",
        ),
        ToolSpec(
            name="bulk_autoroute",
            description=(
                "Route the whole board at once with Freerouting. Right for the housekeeping nets "
                "that just need to get there; wrong for anything with a length, impedance or "
                "isolation constraint — route those interactively first, because this will "
                "cheerfully run a switching node under an analog input. The board is snapshotted "
                "first and DRC afterwards decides whether the result stays."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "passes": {
                        "type": "integer",
                        "description": "Optimisation passes; more takes longer. Default 10.",
                    }
                },
            },
            kind="kicad_mutation",
            handler=_bulk_route,
            approval="ask_always",
        ),
    ]


def sim_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="simulate_subcircuits",
            description=(
                "Run SPICE over the analog subcircuits the review detected — RC/LC filters, "
                "voltage dividers, opamp stages, crystal load networks — and compare each "
                "against what its topology was supposed to do. Reach for this when the user "
                "asks whether a filter, divider or gain stage is actually right, or when a "
                "review reports simulatable subcircuits: the analyzer can only see that a "
                "divider exists, not that it lands on the wrong voltage. Ask for monte_carlo "
                "when the question is whether it still works on real parts rather than "
                "nominal ones. The reply says whether PCB parasitics were included — on an "
                "unrouted board they are not, so a pass is about the topology, not the layout."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Subcircuit types to simulate; omit for all detected.",
                    },
                    "monte_carlo": {
                        "type": "integer",
                        "description": "Tolerance samples per subcircuit, e.g. 100. Omit for nominal only.",
                    },
                },
            },
            kind="file_mutation",
            handler=_simulate,
            approval="ask",
        ),
    ]


# ---------------------------------------------------------------------------
# The set
# ---------------------------------------------------------------------------


def project_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_project_files",
            description=(
                "List the project's design documents, its generated pipeline artifacts, and any "
                "cached datasheet PDFs. Start here when you do not know what the project contains."
            ),
            input_schema={"type": "object", "properties": {}},
            kind="query",
            handler=_list_files,
        ),
        ToolSpec(
            name="read_project_file",
            description="Read one design document from the project root, e.g. 'overview.md'.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Filename in the project root."}},
                "required": ["path"],
            },
            kind="file_read",
            handler=_read_file,
            path_args=("path",),
        ),
        ToolSpec(
            name="read_pipeline_artifact",
            description=(
                "Read a generated artifact from .pipeline/ — design_artifact.deterministic.json, "
                "bom.json, nets.json, coverage_report.json, review_report.json, and so on. This is "
                "what the pipeline actually produced; prefer it over assumptions when diagnosing."
            ),
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Filename inside .pipeline/."}},
                "required": ["name"],
            },
            kind="file_read",
            handler=_read_artifact,
        ),
        ToolSpec(
            name="propose_file_edit",
            description=(
                "Propose new content for a design document. The user reviews it as a diff and "
                "accepts or rejects — nothing is written until they do. Supply the COMPLETE new "
                "file content. Read the file first unless you are creating it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Filename in the project root (.md/.yaml)."},
                    "new_content": {"type": "string", "description": "The complete new file content."},
                    "rationale": {"type": "string", "description": "What changed and why, briefly."},
                },
                "required": ["path", "new_content", "rationale"],
            },
            # A proposal writes nothing on its own — the accept step is the
            # approval, and asking twice for one decision trains people to click.
            kind="file_read",
            handler=_propose_edit,
            path_args=("path",),
            write_args=("path",),
        ),
    ]


def parts_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="search_parts",
            description=(
                "Resolve a manufacturer part number across every configured distributor "
                "(DigiKey, Mouser, element14, LCSC) and report what each says: manufacturer, "
                "description, datasheet URL. Use this when a BOM row's MPN is ambiguous, when you "
                "need to confirm a part is real before committing it to the design, or when the "
                "user asks about availability. The reply distinguishes distributors that were not "
                "configured from those that looked and found nothing — do not read an empty result "
                "as 'this part does not exist' without checking which."
            ),
            input_schema={
                "type": "object",
                "properties": {"mpn": {"type": "string", "description": "Manufacturer part number."}},
                "required": ["mpn"],
            },
            kind="network",
            handler=_search_parts,
            # No approval: this reads a public distributor catalogue and changes
            # nothing. Asking for it bought no safety and cost attention — and
            # an approval prompt people learn to click through is worse than no
            # prompt, because it also trains them through the ones that matter.
            #
            # The condition that makes this safe is provenance, and the result
            # carries it: every hit names the distributor it came from and its
            # datasheet URL, and `skipped` versus `not_found` separates "nobody
            # asked" from "asked and told no". Where a number came from is
            # always answerable after the fact.
            approval="auto",
        ),
        ToolSpec(
            name="fetch_datasheet",
            description=(
                "Download a part's datasheet PDF into the project's datasheets/ directory. Do this "
                "before extracting specs. The reply says whether the downloaded file was verified "
                "to actually be that part's datasheet."
            ),
            input_schema={
                "type": "object",
                "properties": {"mpn": {"type": "string"}},
                "required": ["mpn"],
            },
            kind="network",
            handler=_fetch_datasheet,
            # Also auto, with one honest caveat: this writes, where search_parts
            # does not. What it writes is a fetched PDF into the project's
            # datasheets/ cache — it never touches a design file, so it does not
            # cross the boundary the approval prompt exists to guard, and the
            # path still goes through the sandbox first. The file on disk is its
            # own provenance record.
            approval="auto",
        ),
        ToolSpec(
            name="extract_datasheet_specs",
            description=(
                "Read a downloaded datasheet PDF and extract structured specs and pinouts into "
                "the project's cache. Expensive — it reads the PDF's pages with a vision model — "
                "so use it when the design genuinely needs the part's real pinout or absolute "
                "maximums, not to satisfy curiosity. Requires fetch_datasheet first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mpn": {"type": "string"},
                    "file": {
                        "type": "string",
                        "description": (
                            "Optional filename in datasheets/ to read, when it is not "
                            "named after the MPN. Vendors rarely name a PDF after the "
                            "orderable part number, and the user naming a file is a "
                            "statement rather than a guess — prefer it when they do."
                        ),
                    },
                },
                "required": ["mpn"],
            },
            kind="dispatch",
            handler=_extract_datasheet,
            approval="ask_always",
        ),
        ToolSpec(
            name="read_datasheet_specs",
            description=(
                "Read specs already extracted for a part — pinout, absolute maximums, electrical "
                "characteristics — with each value's confidence and the page it came from. Cheap; "
                "try this before extracting again."
            ),
            input_schema={
                "type": "object",
                "properties": {"mpn": {"type": "string"}},
                "required": ["mpn"],
            },
            kind="file_read",
            handler=_read_extraction,
        ),
    ]


def kicad_tools(url: str | None) -> list[ToolSpec]:
    """KiCad editing tools, when a bridge is configured and offers what BLPL
    needs. An unreachable or too-old server contributes nothing rather than a
    partial set — discovering there are no vias three-quarters through routing
    a board is worse than not starting.

    The viewer highlight is always available: it needs no server, and pointing
    at the part under discussion is useful in every conversation.
    """
    from .kicad_bridge import build_tools, highlight_tool, probe

    tools = [highlight_tool()]
    if not url:
        return tools
    status, usable = probe(url)
    if not status.available:
        return tools
    return tools + build_tools(url, usable)


def default_tools(kicad_url: str | None = None) -> list[ToolSpec]:
    return (
        project_tools()
        + parts_tools()
        + module_tools()
        + sim_tools()
        + fab_tools()
        + kicad_tools(kicad_url)
    )
