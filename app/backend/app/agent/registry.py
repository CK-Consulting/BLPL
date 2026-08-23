"""The tools a design conversation can call, and what each is allowed to do.

Grouped by what they touch, because that is what decides their policy:

  *project* — read anything in the project, propose an edit. Reads are
              automatic; the "write" is a proposal a human accepts, so it needs
              no approval of its own.
  *parts*   — reach distributor APIs. Network egress with your API keys on it,
              so it asks once per session.
  *depth*   — datasheet download and extraction. Spends real money per call and
              writes into the project's cache, but reads nothing the project
              does not already contain, so it runs without asking.

Reading is free inside the project directory, and that is the whole rule. The
boundary this enforces is the project, the same shape as a web server rooted at
a document directory: everything under it is reachable, nothing above it is.
Confirming individual reads inside that boundary bought no safety — the files
are the user's own, put there for this — and cost the thing approvals actually
run on, which is someone still reading them.

Writing is a separate question with a separate answer. It stays gated, and on a
multi-board project the write scope narrows further to the board an agent owns.

The descriptions are written for the model and say *when* to reach for a tool,
not just what it does — a tool description that only states its function gets
called at the wrong moments, and this set has expensive members.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from blpl.agent.tools.bom import HOUSES as HOUSES_FOR_SCHEMA
from blpl.agent.tools.parts import fetch_datasheet, search_parts

from .toolspec import ToolContext, ToolDenied, ToolSpec

# Design documents, matching the editor's rule so the assistant can never
# propose a file the user has no way to open.
EDITABLE_SUFFIXES = {".md", ".markdown", ".yaml", ".yml", ".mmd", ".mermaid"}

# Files the user keeps in the project but does not want in the design
# conversation. Not hidden, not git-ignored, not off-limits — the boundary the
# app enforces is the project directory, and this folder is inside it. What it
# changes is *default attention*: its contents are named in a listing but never
# swept up as project context, and are read only when someone points at one.
#
# The distinction matters because the alternatives are all worse. Deleting the
# files loses them; git-ignoring them takes them out of the history the project
# depends on; making them unreadable means the one time you do want the
# assistant to look at the mechanical drawing, you cannot ask.
CONTEXT_IGNORE = "context-ignore"

# Directories that are never part of a file listing: machinery, not content.
_SKIP_DIRS = {".git", ".pipeline", ".worktrees", "__pycache__", "node_modules"}

# Reading one of these as text produces mojibake, not information. Named
# explicitly so the refusal can say what to do instead, rather than returning a
# screenful of replacement characters and letting the model reason about it.
_OPAQUE_SUFFIXES = {
    ".pdf": "use fetch_datasheet / extract_datasheet_specs for datasheets",
    ".zip": "an archive — ask the user to extract what you need",
    ".png": "an image — attach it to the conversation to have it looked at",
    ".jpg": "an image — attach it to the conversation to have it looked at",
    ".jpeg": "an image — attach it to the conversation to have it looked at",
    ".step": "a 3D model — not readable as text",
    ".stp": "a 3D model — not readable as text",
    ".xlsx": "a spreadsheet — export it to CSV first",
    ".docx": "a Word document — export it to text or markdown first",
}

# Enough for any design document or netlist; short of pulling a generated
# multi-megabyte artifact into the conversation whole.
_READ_LIMIT = 256 * 1024


def _readable_file(ctx: ToolContext, name: str) -> Path:
    """Resolve a path for *reading*, anywhere inside the project.

    The sandbox already treats the project directory as readable in full, and
    that is the boundary the app actually maintains — the same shape as a web
    server rooted at a document directory. What used to sit on top of it was a
    second, much tighter rule in this module: bare filenames, project root,
    markdown only. So a datasheet the user had put in the project, or any file
    in a sub-board's directory, was unreachable by the assistant working on it.

    Writing is unchanged and still goes through ``_project_file``: reading a
    file and editing it are not the same permission, and only one of them is
    recoverable by pressing undo.
    """
    raw = (name or "").strip().replace("\\", "/")
    if not raw:
        raise ToolDenied("path must name a file inside the project")
    if ".." in Path(raw).parts:
        raise ToolDenied(f"{name!r} must stay inside the project directory")
    root = ctx.project_dir.resolve()
    if raw.startswith("/"):
        # An absolute path is accepted only when it is this project's own — a
        # model that has seen the project root in an earlier tool result will
        # sometimes echo it back. Anything else is refused outright rather than
        # quietly reinterpreted as relative, which would turn '/etc/passwd' into
        # a confusing "no such file in this project" instead of a straight no.
        absolute = Path(raw)
        if not absolute.is_relative_to(root):
            raise ToolDenied(f"{name!r} is outside the project directory")
        raw = str(absolute.relative_to(root))
    target = (root / raw).resolve()
    # Belt and braces with the sandbox: this catches a symlink pointing out of
    # the project, which a string check on the input never would.
    if not target.is_relative_to(root):
        raise ToolDenied(f"{name!r} resolves outside the project directory")
    return target


def _read_text(target: Path) -> str:
    """Read a project file as text, or say precisely why it cannot be."""
    hint = _OPAQUE_SUFFIXES.get(target.suffix.lower())
    if hint:
        raise ToolDenied(f"{target.name} is not readable as text — {hint}")
    size = target.stat().st_size
    if size > _READ_LIMIT:
        raise ToolDenied(
            f"{target.name} is {size // 1024} KB, over the {_READ_LIMIT // 1024} KB read limit. "
            "Read a generated artifact through read_pipeline_artifact, or ask the user which "
            "part of it matters."
        )
    head = target.read_bytes()[:8192]
    if b"\x00" in head:
        raise ToolDenied(f"{target.name} looks binary — it has no text to read")
    return target.read_text(encoding="utf-8", errors="replace")


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


def _walk(root: Path, *, skip: set[str], limit: int = 400) -> list[str]:
    """Every file under ``root`` as a project-relative path, machinery omitted.

    Bounded rather than complete. A listing that grows without limit is a
    listing that one day arrives as fifty thousand paths and displaces the
    conversation it was meant to inform, and truncating silently would read as
    "that is everything" — so the caller reports the count it dropped.
    """
    out: list[str] = []
    stack = [root]
    while stack:
        here = stack.pop()
        try:
            entries = sorted(here.iterdir())
        except OSError:
            continue
        for f in entries:
            if f.is_dir():
                if f.name in skip or f.name.startswith("."):
                    continue
                stack.append(f)
            elif f.is_file() and not f.name.startswith("."):
                out.append(str(f.relative_to(root)))
                if len(out) >= limit * 4:  # hard stop; the caller trims to limit
                    return sorted(out)
    return sorted(out)


async def _list_files(ctx: ToolContext, args: dict) -> str:
    root = ctx.project_dir.resolve()
    docs = sorted(
        f.name
        for f in root.iterdir()
        if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in EDITABLE_SUFFIXES
    )
    pipeline = root / ".pipeline"
    arts = sorted(f.name for f in pipeline.iterdir() if f.is_file()) if pipeline.is_dir() else []
    # Recursive, and not only PDFs. Per-MPN subdirectories are the established
    # convention — the datasheet resolver already looks for them — and the text
    # extractions and pinmaps that sit beside a PDF are what the extraction
    # stages actually consume. A non-recursive ``*.pdf`` glob showed neither,
    # and because everything under ``datasheets/`` is excluded from `others`
    # below, a file in a subdirectory appeared in *no* listing at all. The agent
    # was then reduced to guessing paths, and retried the same wrong guess until
    # the turn burned out.
    sheets = root / "datasheets"
    found = _walk(sheets, skip=_SKIP_DIRS) if sheets.is_dir() else []
    pdfs = found[:200]
    sheets_dropped = max(0, len(found) - 200)

    # Everything else in the project, sub-board directories included. Previously
    # absent, which made a multi-board project look empty below its root.
    known = set(docs) | {f"datasheets/{p}" for p in pdfs}
    others = [
        p
        for p in _walk(root, skip=_SKIP_DIRS | {CONTEXT_IGNORE})
        if p not in known and not p.startswith("datasheets/")
    ]
    dropped = max(0, len(others) - 400)
    payload: dict = {
        "design_documents": docs,
        "other_project_files": others[:400],
        "pipeline_artifacts": arts,
        "datasheets": pdfs,
    }
    if dropped:
        payload["other_project_files_omitted"] = dropped
    if sheets_dropped:
        payload["datasheets_omitted"] = sheets_dropped

    ignored = root / CONTEXT_IGNORE
    if ignored.is_dir():
        names = _walk(ignored, skip=_SKIP_DIRS)
        # Names, not contents, and never swept up as context. Without the names
        # the folder could not be used at all — "look at the enclosure drawing"
        # needs something to resolve against — and a filename is cheap where the
        # file behind it is the thing that would fill the conversation.
        payload["context_ignore"] = {
            "note": (
                f"The user keeps these in {CONTEXT_IGNORE}/ because they are not part of the "
                "board design work. Do not read them or reason from them unless the user asks "
                "about one by name. They are readable when they do."
            ),
            "files": [f"{CONTEXT_IGNORE}/{n}" for n in names[:100]],
            "omitted": max(0, len(names) - 100),
        }
    return json.dumps(payload, indent=2)


async def _read_file(ctx: ToolContext, args: dict) -> str:
    from ..chat import sha_of  # local import: chat owns the proposal hashing

    target = _readable_file(ctx, str(args.get("path", "")))
    ctx.sandbox.check_read(target)
    if target.is_dir():
        # "no file X in this project" is false when X is a directory that plainly
        # exists, and a false error is one the model argues with: it retried the
        # same path with and without a trailing slash a dozen times rather than
        # believe it. So say what the path really is and name what is inside —
        # an error that carries the answer ends the guessing in one turn.
        inside = sorted(
            f"{f.name}/" if f.is_dir() else f.name
            for f in target.iterdir()
            if not f.name.startswith(".")
        )
        shown = ", ".join(inside[:40]) or "(empty)"
        more = f" … and {len(inside) - 40} more" if len(inside) > 40 else ""
        raise IsADirectoryError(
            f"{args.get('path')!r} is a directory, not a file. It contains: {shown}{more}. "
            "Read one of those by its full path."
        )
    if not target.is_file():
        raise FileNotFoundError(f"no file {args.get('path')!r} in this project")
    text = _read_text(target)
    # Keyed by the path as given, so a later proposal for the same file finds
    # the hash of the bytes that were actually read. Root-level documents — the
    # only ones a proposal can target — key the same way they always did.
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
    from blpl.agent.tools import datasheet_files

    # Into the part's own folder. A file this tool fetched for a specific MPN is
    # the least ambiguous case there is, and filing it under the part number
    # records that at the moment it is known — rather than writing
    # "<MPN>.pdf" at the top level and having every later lookup re-derive it
    # from the filename.
    dest = datasheet_files.part_dir(ctx.project_dir, mpn)
    ctx.sandbox.check_write(dest)
    dest.mkdir(parents=True, exist_ok=True)
    ctx.note(f"downloading datasheet for {mpn}")
    result = await _to_thread(fetch_datasheet, mpn, dest, creds=ctx.creds)
    return json.dumps(result.to_dict(), indent=2)


def usable_extraction(payload: object) -> bool:
    """Whether an extraction holds findings rather than a record of failing.

    A partial merge marks each task it could not complete with
    ``{"_extraction_failed": true, "reason": ...}``. That is the right thing to
    write into a project's cache — it says what happened, next to what worked —
    and exactly the wrong thing to keep in a library that is consulted first and
    across projects, because it converts one bad afternoon into a permanent
    answer.

    Checked recursively: the sentinel appears at whatever depth the failing task
    sat, so a top-level look would miss most of them.
    """
    if isinstance(payload, dict):
        if payload.get("_extraction_failed"):
            return False
        return all(usable_extraction(v) for v in payload.values())
    if isinstance(payload, list):
        return all(usable_extraction(v) for v in payload)
    return True


async def _extract_datasheet(ctx: ToolContext, args: dict) -> str:
    from blpl.agent.tools.datasheets import extract_datasheet

    mpn = str(args.get("mpn", "")).strip()
    if not mpn:
        raise ToolDenied("mpn is required")
    # Before anything else, including the vision-endpoint requirement. An
    # extraction already paid for on another of this user's projects is the same
    # answer — a pinout is a property of the part, not of the board — and
    # demanding a model that can see, in order to hand back a result that was
    # read months ago, is a check standing in front of nothing.
    if ctx.library is not None:
        prior = ctx.library.get(mpn)
        if prior is not None:
            ctx.note(f"{mpn}: reusing the extraction already in your component library")
            return json.dumps({"source": "component-library", "mpn": mpn, **prior}, indent=2)

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
    if found.how in ("explicit", "family"):
        # Remember what was worked out, so the next run is a lookup rather than
        # another guess — and so a person can see and correct the binding.
        #
        # "family" is the guess worth remembering, and it was spelled "prefix"
        # here — a value `resolve` has never returned, so the working-out was
        # thrown away every time and only an explicitly named file was ever
        # written down.
        #
        # Recorded relative to datasheets/, because the name alone cannot find a
        # file in a subdirectory again: the map is consulted before any matching
        # runs, so a row that resolves to nothing does not fall through to the
        # search that would have succeeded — it answers for it.
        try:
            rel = pdf.resolve().relative_to((ctx.project_dir / "datasheets").resolve())
            datasheet_files.record(ctx.project_dir, mpn, rel.as_posix())
        except (OSError, ValueError):
            pass
    if found.how != "exact":
        ctx.note(f"{mpn}: reading {pdf.name} (matched by {found.how})")

    # Which kind of model this needs is a property of the document, not of the
    # task. A datasheet with a text layer — which is nearly all of them — is
    # read by any competent text model; only a scan needs one that can see.
    #
    # Deciding it here rather than demanding vision up front is what makes the
    # ordinary case work at all. Requiring a vision endpoint for every
    # extraction meant a perfectly readable PDF failed because no vision route
    # was configured, and when one was, it was a small VL model that could not
    # hold the output schema: 0 of 7 pinouts, malformed JSON every time.
    from blpl.agent.tools.datasheets import has_text_layer

    readable = await _to_thread(has_text_layer, pdf)
    chain = ctx.endpoints_for("chat" if readable else "vision")
    if not chain and readable:
        chain = ctx.endpoints_for("default")
    if not chain:
        raise ToolDenied(
            f"{pdf.name} has no text layer, so it has to be read as images, and no "
            "vision-capable endpoint is routed to the 'vision' task. Set one in Settings."
            if not readable
            else "no endpoint is routed to chat or default — set one in Settings."
        )

    cache = ctx.project_dir / "datasheets" / "extracted"
    ctx.sandbox.check_write(cache)
    # The whole chain, not its head. A model that cannot hold the output schema
    # fails every task the same way, so the endpoints behind it are the fix.
    run = await extract_datasheet(mpn, pdf, cache, chain, on_progress=ctx.note)
    # Kept for next time, and for the next project — but only when something
    # actually landed.
    #
    # This checked that the merged file *existed*, which after the partial-merge
    # change it does even when every task failed: the merge writes
    # {"_extraction_failed": true, "reason": "<whatever the provider said>"} in
    # place of each one. So a run that failed got filed, permanently, per user,
    # across every project — and because the library is consulted before
    # anything else, it then answered every later attempt with the fossilised
    # error instead of trying again.
    #
    # The symptom was a provider that had been removed from every route still
    # appearing in failures: "your credit balance is too low to access the
    # Anthropic API", quoted back weeks later from a cache, on a system with no
    # Anthropic route at all.
    merged = cache / f"{mpn}.json"
    if ctx.library is not None and run.ok and merged.is_file():
        try:
            payload = json.loads(merged.read_text(encoding="utf-8"))
            if usable_extraction(payload):
                ctx.library.put(mpn, payload)
                ctx.note(f"{mpn}: saved to your component library for reuse")
        except (OSError, json.JSONDecodeError):
            # Never fatal: the extraction is on disk in the project either way.
            # Failing to file a copy is worth less than the result.
            pass
    return json.dumps(run.to_dict(), indent=2)


# ---------------------------------------------------------------------------
# History
#
# The project is a git repository and every accepted proposal is a commit, so
# the record of what a file used to say already exists. Nothing could read it.
#
# That gap is what turns an ordinary context limit into lost work. A pinmap
# agreed forty turns ago falls out of the window — summarisation drops tables
# first — and with no way to consult the history, the only remaining copy of it
# is the one in the conversation nobody can search. The bytes were never gone;
# the door was missing.
# ---------------------------------------------------------------------------

# Refs come from a model, so they are constrained rather than trusted. This
# admits hashes, HEAD, HEAD~3, branch names and tags, and refuses anything
# beginning with '-' — a ref that is really a git option is the injection to
# care about here. Paths are always passed after '--' for the same reason.
_REF = re.compile(r"^(?!-)[A-Za-z0-9_./~^@{}-]{1,64}$")


def _git(project_dir: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        raise ToolDenied(f"git {args[0]}: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout


def _ref(value: str) -> str:
    ref = (value or "").strip()
    if not _REF.match(ref):
        raise ToolDenied(f"{value!r} is not a usable commit reference")
    return ref


async def _file_history(ctx: ToolContext, args: dict) -> str:
    raw = str(args.get("path", "")).strip()
    limit = max(1, min(int(args.get("limit") or 20), 100))
    argv = ["log", f"-{limit}", "--date=iso-strict", "--format=%h\t%ad\t%an\t%s"]
    if raw:
        # Resolved through the same rule as a read, so history cannot be asked
        # for a path a read would refuse.
        target = _readable_file(ctx, raw)
        ctx.sandbox.check_read(target)
        argv += ["--", str(target.relative_to(ctx.project_dir.resolve()))]
    out = await _to_thread(_git, ctx.project_dir, *argv)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            rows.append({"commit": parts[0], "when": parts[1], "who": parts[2], "what": parts[3]})
    if not rows:
        return json.dumps(
            {
                "commits": [],
                "note": (
                    f"no commits touch {raw!r}"
                    if raw
                    else "this project has no history yet"
                ),
            },
            indent=2,
        )
    return json.dumps({"commits": rows}, indent=2)


async def _read_file_version(ctx: ToolContext, args: dict) -> str:
    target = _readable_file(ctx, str(args.get("path", "")))
    ctx.sandbox.check_read(target)
    ref = _ref(str(args.get("commit", "")))
    rel = str(target.relative_to(ctx.project_dir.resolve()))
    text = await _to_thread(_git, ctx.project_dir, "show", f"{ref}:{rel}")
    if len(text) > _READ_LIMIT:
        raise ToolDenied(
            f"{rel} at {ref} is {len(text) // 1024} KB, over the "
            f"{_READ_LIMIT // 1024} KB read limit"
        )
    return text


async def _diff_file(ctx: ToolContext, args: dict) -> str:
    since = _ref(str(args.get("since", "")))
    until = _ref(str(args.get("until") or "HEAD")) if args.get("until") else None
    argv = ["diff", "--unified=3", since] + ([until] if until else [])
    raw = str(args.get("path", "")).strip()
    if raw:
        target = _readable_file(ctx, raw)
        ctx.sandbox.check_read(target)
        argv += ["--", str(target.relative_to(ctx.project_dir.resolve()))]
    out = await _to_thread(_git, ctx.project_dir, *argv)
    if not out.strip():
        return f"no changes to {raw or 'the project'} between {since} and {until or 'the working tree'}"
    if len(out) > _READ_LIMIT:
        out = out[:_READ_LIMIT] + f"\n... diff truncated at {_READ_LIMIT // 1024} KB"
    return out


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
                "List everything in the project: design documents, files in sub-board and other "
                "directories, generated pipeline artifacts, and cached datasheet PDFs. Start here "
                "when you do not know what the project contains. Anything listed under "
                f"'context_ignore' is deliberately outside the design work — do not read those "
                "unless the user asks about one."
            ),
            input_schema={"type": "object", "properties": {}},
            kind="query",
            handler=_list_files,
        ),
        ToolSpec(
            name="read_project_file",
            description=(
                "Read any text file in the project by its path — 'overview.md', "
                "'sensor/board.md', 'notes/power-budget.csv'. Use list_project_files if you do "
                "not know what is there. Reads are free and need no permission; the project "
                "directory is the boundary."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path relative to the project root. May name a subdirectory, "
                            "e.g. 'sensor/board.md'."
                        ),
                    }
                },
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
        ToolSpec(
            name="file_history",
            description=(
                "When a file changed and why. Every accepted edit is a commit, so this is the "
                "record of how the design got to where it is. Reach for it when the user refers "
                "to something from earlier — 'the pinmap that worked', 'before we changed the "
                "rail' — instead of saying you no longer have it: the conversation may have been "
                "summarised, the history has not. Omit path for the whole project."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional file, relative to the project root."},
                    "limit": {"type": "integer", "description": "How many commits, newest first (default 20)."},
                },
            },
            kind="file_read",
            handler=_file_history,
        ),
        ToolSpec(
            name="read_file_version",
            description=(
                "Read a file exactly as it stood at a past commit. Use file_history first to "
                "find the commit. This is how a value that was agreed and later overwritten is "
                "recovered — quote it rather than reconstructing it from memory."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File, relative to the project root."},
                    "commit": {"type": "string", "description": "A commit hash from file_history, or HEAD~2."},
                },
                "required": ["path", "commit"],
            },
            kind="file_read",
            handler=_read_file_version,
        ),
        ToolSpec(
            name="diff_file",
            description=(
                "What changed in a file since a past commit, as a unified diff. Cheaper than "
                "reading both versions when the question is what moved rather than what it says."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional file; omit for the whole project."},
                    "since": {"type": "string", "description": "The commit to compare against."},
                    "until": {
                        "type": "string",
                        "description": "Optional second commit. Omit to compare against the files as they are now.",
                    },
                },
                "required": ["since"],
            },
            kind="file_read",
            handler=_diff_file,
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
            # Reading a file the project already contains is not a decision worth
            # interrupting anyone for. This asked every single time, and the cost
            # was not the seconds: the question arrives mid-answer, in a panel
            # you may have scrolled away from, about a datasheet you put in the
            # project yourself for exactly this purpose. A prompt like that is
            # not a safety control, it is a control people learn to click
            # through — which then spends the attention the prompts that
            # matter were relying on.
            #
            # What it was really guarding was *spend*, not safety, and spend is
            # answerable after the fact: the extraction is written to the
            # project's cache with the model and page range that produced it,
            # and the usage ledger records the call. read_datasheet_specs is
            # cheap and is described as the thing to try first.
            approval="auto",
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
