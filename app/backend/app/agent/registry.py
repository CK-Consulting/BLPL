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
    pdf = ctx.project_dir / "datasheets" / f"{mpn}.pdf"
    if not pdf.is_file():
        raise FileNotFoundError(f"no datasheet at {pdf.name} — call fetch_datasheet first")
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
            approval="ask",
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
            approval="ask",
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
                "properties": {"mpn": {"type": "string"}},
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


def default_tools() -> list[ToolSpec]:
    return project_tools() + parts_tools()
