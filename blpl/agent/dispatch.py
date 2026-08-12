"""`python -m blpl.agent.dispatch` — batch agent work as a durable run.

Long jobs (extracting a 40-part BOM's datasheets) belong in the batch lane: a
subprocess under RunManager, so they inherit durability, log replay, stop, and
boot recovery instead of dying with whatever browser tab started them. The
interactive lane in app/chat.py handles the short, approval-shaped work.

Credentials arrive the way stage subprocesses already get them — in the
environment, never on a command line — so a `ps` listing during a run shows
nothing worth stealing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from blpl.core.llm_chat import Endpoint


def _endpoint_from_env() -> Endpoint:
    """Rebuild the routed endpoint the app resolved for this task.

    The parent injects HDM_LLM_CHAIN (the same shape stages get), so the batch
    lane honours per-task routing without re-implementing the resolver.
    """
    chain = json.loads(os.environ.get("HDM_LLM_CHAIN", "[]"))
    if not chain:
        raise SystemExit("no HDM_LLM_CHAIN in the environment — nothing to run with")
    first = chain[0]
    key_env = first.get("key_env") or ""
    return Endpoint(
        name=first.get("endpoint") or first.get("provider", ""),
        kind=first.get("provider", "anthropic"),
        model=first.get("model", ""),
        api_key=os.environ.get(key_env) if key_env else None,
        base_url=first.get("base_url") or None,
    )


def _cmd_datasheets(args: argparse.Namespace) -> int:
    from blpl.agent.tools.datasheets import extract_datasheet

    project = Path(args.project_dir).resolve()
    cache = project / "datasheets" / "extracted"
    endpoint = _endpoint_from_env()
    print(f"dispatch: {len(args.mpn)} part(s) via {endpoint.name} ({endpoint.model})", flush=True)

    failures = 0
    for mpn in args.mpn:
        pdf = Path(args.pdf_dir or (project / "datasheets")) / f"{mpn}.pdf"
        if not pdf.is_file():
            # Reported, never skipped silently: a part with no PDF is a gap in
            # the run, and the summary must say so.
            print(f"  {mpn}: no PDF at {pdf} — skipped", file=sys.stderr, flush=True)
            failures += 1
            continue
        run = asyncio.run(
            extract_datasheet(
                mpn, pdf, cache, endpoint,
                force=args.force,
                retry_failed=args.retry_failed,
                on_progress=lambda m: print(f"  {m}", flush=True),
            )
        )
        summary = run.to_dict()
        print(f"  {mpn}: {json.dumps(summary)}", flush=True)
        if not run.ok:
            failures += 1
    print(f"dispatch: {len(args.mpn) - failures}/{len(args.mpn)} succeeded", flush=True)
    return 1 if failures else 0


def _cmd_review_panel(args: argparse.Namespace) -> int:
    from blpl.agent import review_panel

    project = Path(args.project_dir).resolve()
    pipeline = project / ".pipeline"
    entries = review_panel.panel_entries()
    print(
        f"panel: {len(entries)} reviewer(s): "
        + ", ".join(e.get("endpoint") or e.get("provider", "?") for e in entries),
        flush=True,
    )

    adjudicator = None
    if len(entries) > 1 and not args.no_adjudicator:
        # The cheapest routed endpoint arbitrates near-duplicates. Last in the
        # chain is the cheapest by the convention the chain is ordered on, and
        # the job — "are these two sentences the same finding?" — does not need
        # the strongest model on the panel.
        adjudicator = _make_adjudicator(entries[-1])

    report = review_panel.run(pipeline, entries=entries, adjudicator=adjudicator)
    path = review_panel.write_report(pipeline, report)

    for member in report["panel"]:
        state = f"failed: {member['error']}" if member["error"] else f"{member['findings']} finding(s)"
        print(f"  {member['model_id']}: {state}", flush=True)
    s = report["summary"]
    print(
        f"panel: {s['findings']} finding(s) from {s['answered']}/{s['panelists']} reviewers "
        f"({s['agreed']} corroborated, {s['single_source']} single-source) → {path.name}",
        flush=True,
    )
    if report.get("trust_summary"):
        print(f"panel: {report['trust_summary']}", file=sys.stderr, flush=True)
    # A panel that produced findings has done its job; errors in the board are
    # the report's content, not this command's failure.
    return 0 if s["answered"] else 1


def _make_adjudicator(entry: dict):
    from blpl.core import llm_adapter

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["same"],
        "properties": {
            "same": {"type": "boolean"},
            "why": {"type": "string"},
        },
    }
    key_env = entry.get("key_env") or ""
    adapter = llm_adapter.build_adapter(
        entry.get("provider") or entry.get("kind"),
        entry.get("model") or None,
        api_key=os.environ.get(key_env) if key_env else None,
        base_url=entry.get("base_url") or None,
    )

    def adjudicate(a: dict, b: dict) -> bool:
        result = adapter.complete_json(
            "Two reviewers of the same board wrote these findings. Answer only whether they "
            "describe the SAME underlying problem, such that fixing one fixes the other. "
            "When unsure, answer false: leaving a duplicate on the report costs a reader a "
            "moment, and wrongly merging loses a finding.",
            json.dumps({"a": a["summary"], "b": b["summary"]}, indent=2),
            schema,
        )
        return bool(result.get("same"))

    return adjudicate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blpl.agent.dispatch")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("datasheets", help="Extract structured specs from cached datasheet PDFs.")
    p.add_argument("--project-dir", required=True)
    p.add_argument("--mpn", action="append", required=True, help="Repeatable.")
    p.add_argument("--pdf-dir", help="Where the PDFs are (default <project>/datasheets).")
    p.add_argument("--force", action="store_true", help="Re-extract even where results exist.")
    p.add_argument("--retry-failed", action="store_true", help="Re-run only failed tasks.")
    p.set_defaults(func=_cmd_datasheets)

    r = sub.add_parser(
        "review-panel",
        help="Review the pipeline artifacts with every endpoint routed to review_panel.",
    )
    r.add_argument("--project-dir", required=True)
    r.add_argument(
        "--no-adjudicator",
        action="store_true",
        help="Skip the near-duplicate merge step; leaves similar findings separate.",
    )
    r.set_defaults(func=_cmd_review_panel)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
