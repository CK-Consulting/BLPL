"""The datasheet extraction dispatcher.

kicad-happy's datasheets skill is deliberately half-built: it ships the planner,
the schema validators, the merge step and the quality scoring, and specifies —
in ``references/dispatcher-contract.md`` — the one piece it does not ship. That
piece is a thing that can hand PDF pages to a vision-capable model and write
back schema-valid results. The contract exists because the skill was written to
be driven by *some* host; this server is that host.

The flow, with the piece this module owns marked:

    scout subagent            ← here (needs a model that can read pages)
      → plan_extraction.py    ← kicad-happy
        → N extractor subagents ← here, one per task, in parallel
          → merge_results.py  ← kicad-happy
            → lookup(mpn)     ← kicad-happy

Why this needs Phase 2's task routing: a datasheet is read as images. Routing
extraction at a text-only endpoint does not error — the model simply describes
nothing and the schema validator rejects empty output, or worse, accepts a
plausible hallucination. ``vision`` is a declared vision task and the
config refuses to route it anywhere blind.

Contract obligations kept here, because each one has a failure it prevents:
idempotence (a rerun after a crash must not re-bill work already done),
never overwriting a complete result without --force, schema validation before
claiming success, and a cost ledger so a 40-part BOM's price is visible rather
than discovered on an invoice.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core import limits, llm_chat
from ...core.llm_chat import DocumentBlock, Endpoint, Msg, TextBlock, build_chat_adapter
from ..kicad_happy import find_kicad_happy, run_script, script_path

# Anything above this many pages is a family datasheet or a reference manual;
# sending it whole wastes a fortune and buries the pages that matter. The
# planner's page selection is what keeps extraction affordable.
_MAX_PAGES_PER_TASK = 24


@dataclass
class TaskResult:
    task_id: str
    status: str            # complete | failed | skipped
    error: str = ""
    model_id: str = ""
    tokens_in: int = 0
    tokens_out: int = 0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "error": self.error,
            "model_id": self.model_id,
        }


@dataclass
class ExtractionRun:
    mpn: str
    results: list[TaskResult] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        """Whether this run produced something.

        A run with no tasks is not a success. It used to report one: the scout
        would decline a document — "target MPN not found in PDF", because the
        part is a module sold under a distributor SKU and the datasheet calls it
        by its product name — the planner would emit zero tasks, the merge would
        have nothing to object to, and the whole thing came back ``ok: true``
        with an empty result.

        That is worse than an error. An error gets read; a green tick over
        nothing gets believed.
        """
        if self.error:
            return False
        if not self.results:
            return False
        return all(r.status != "failed" for r in self.results)

    def to_dict(self) -> dict:
        return {
            "mpn": self.mpn,
            "ok": self.ok,
            "error": self.error,
            "tasks": [r.to_dict() for r in self.results],
            "complete": sum(1 for r in self.results if r.status == "complete"),
            "failed": sum(1 for r in self.results if r.status == "failed"),
            "skipped": sum(1 for r in self.results if r.status == "skipped"),
        }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def page_text(pdf: Path, pages: Sequence[int] = ()) -> str:
    """The document's own text for these pages, with its column layout kept.

    ``-layout`` rather than the default reading order, because a pin table is
    columns: the whitespace *is* the structure, and a model can read
    "4    SWDIO    Digital I/O" without any recogniser guessing at it.

    Empty when the document has no text layer, which is the signal to fall back
    to sending pages as images. Distinguishing those two is the only thing that
    still needs deciding per document; everything else follows.
    """
    if not shutil.which("pdftotext"):
        return ""
    first = min(pages) if pages else 0
    args = ["pdftotext", "-layout"]
    if pages:
        args += ["-f", str(first), "-l", str(max(pages))]
    try:
        out = subprocess.run(
            args + [str(pdf), "-"], capture_output=True, text=True, timeout=180
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = _mark_pages(out.stdout, first or 1)
    # A page of a scanned PDF yields a form feed and nothing else. Judge on the
    # printable characters rather than the length, or a 900-page scan looks like
    # a document with plenty of text.
    printable = sum(1 for ch in text if ch.strip())
    span = len(pages) if pages else max(1, text.count("\f"))
    return text if printable > 200 * span else ""


def budget_for(endpoint) -> int:
    """How much of a document this endpoint can be handed at once.

    The context window less what the answer needs. Asked of the endpoint rather
    than assumed, because the same model served two ways has two different
    windows — this network's Nemotron reports 512,000 while the model id alone
    would have said 1,000,000 — and because a 4B model on Ollama and a router's
    frontier model are three orders of magnitude apart.
    """
    window = 0
    describe = getattr(limits, "describe", None)
    if describe and getattr(endpoint, "kind", "") in ("openai-compatible", "vllm"):
        found, _ = describe(
            getattr(endpoint, "base_url", "") or "",
            getattr(endpoint, "api_key", None),
            getattr(endpoint, "model", "") or "",
        )
        window = found or 0
    if not window:
        window = int(getattr(endpoint, "context_tokens", 0) or 0)
    if not window:
        window = _CONTEXT_FALLBACK
    return max(4_000, window - limits.output_limit(endpoint, MAX_OUTPUT_TOKENS))


# When nothing will say. Low rather than high: a slice that turns out to fit is
# a wasted round trip, and one that does not is a failed task.
_CONTEXT_FALLBACK = 32_768


# Tried in order; the first that finds anything wins. Ruling lines are the
# strongest signal a PDF gives — they are the table, drawn — and where a
# document rules only its columns, or none of them, the fallbacks infer the rest
# from text alignment.
# Below this, what came back is a caption rather than a table, and the next
# strategy is worth trying. Three is enough to distinguish "header only" from
# "header and some rows" without waiting for a long table to prove itself.
_REAL_TABLE_ROWS = 3

_TABLE_STRATEGIES = (
    {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
    {"vertical_strategy": "lines", "horizontal_strategy": "text"},
    {"vertical_strategy": "text", "horizontal_strategy": "text"},
)


def page_tables(pdf: Path, pages: Sequence[int] = (), *, limit: int = 400) -> str:
    """The tables on these pages, as cells, read out of the PDF itself.

    This is the part that laying the text out flat could never do. A pin table
    is a grid, and a PDF says so — it has the ruling lines, the cell bounds and
    the text runs inside them. Rendering it to fixed-width text and asking a
    model to infer the grid back throws that away, and then every downstream
    trick is an attempt to recover information the file had already given us.

    What it recovers that `-layout` and OCR both lost:

        pdfplumber   ['6', 'P0.22\nTRACEDATA[0]', 'Digital I/O (SoC)\nTrace data', …]
        -layout       6  P0.22  Digital I/O (SoC)  General purpose I/O.
                         TRACEDATA[0]  Trace data  …          ← two lines, no link
        OCR (MinerU) ['TRACECLK P0.22', 'Trace clock Digital l/O (SoC)', …]
                                                              ← merged, and 'l' for 'I'

    A pin with two functions is one cell containing two lines, which is exactly
    what it means. Emitted with a marker rather than a newline so a row stays a
    row.
    """
    try:
        import pdfplumber
    except ImportError:
        return ""
    out: list[str] = []
    rows_emitted = 0
    try:
        with pdfplumber.open(str(pdf)) as doc:
            wanted = list(pages) if pages else range(1, len(doc.pages) + 1)
            for number in wanted:
                if number < 1 or number > len(doc.pages):
                    continue
                page = doc.pages[number - 1]
                # Ruling lines win when a document actually draws them: they
                # are the table, and inferring columns from text alignment is a
                # guess that can split a description in half. But a document
                # that rules only its *header* — the nRF54L15 module spec —
                # yields one row containing the header and nothing else, and
                # taking that on faith left 45 pins unread.
                #
                # So: the first strategy that produces a table rather than just
                # a caption. Falling back on row count alone was worse than
                # either — it preferred a text split with more rows and wrong
                # columns, turning "1 | GND | Ground" into "GND | Ground".
                tables: list[list[list[str | None]]] = []
                best = 0
                for strategy in _TABLE_STRATEGIES:
                    try:
                        found = page.extract_tables(strategy)
                    except Exception:  # noqa: BLE001 — a strategy that throws is a strategy that failed
                        continue
                    rows = sum(len(t) for t in found)
                    if rows > best:
                        tables, best = found, rows
                    if rows >= _REAL_TABLE_ROWS:
                        break
                for table in tables:
                    rendered = _render_table(table)
                    if not rendered:
                        continue
                    out.append(f"[table, page {number}]\n{rendered}")
                    rows_emitted += len(table)
                    if rows_emitted >= limit:
                        out.append(f"[… further tables omitted after {limit} rows]")
                        return "\n\n".join(out)
    except Exception:  # noqa: BLE001 — an unreadable PDF falls back to the text layer
        return ""
    return "\n\n".join(out)


def _render_table(table: list[list[str | None]]) -> str:
    rows = []
    for row in table:
        cells = [(c or "").strip().replace("\n", " ⏎ ") for c in row]
        if not any(cells):
            continue
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def _mark_pages(text: str, first: int) -> str:
    """Label each page break with its real page number.

    pdftotext separates pages with a form feed and says nothing about which page
    is which. The schema asks every finding for the page it came from, so
    without this the model is guessing at the one field that makes a claim
    checkable afterwards.
    """
    out = []
    for i, part in enumerate(text.split("\f")):
        if part.strip():
            out.append(f"--- Page {first + i} ---\n{part}")
    return "\n".join(out)


# What a task is looking for in a table of contents, most specific first — the
# order is the ranking, so "pin assignments" beats "pin configuration" when a
# document has both and only one of them is the pinout.
#
# Only for tasks whose section a datasheet actually names. Anything absent here
# keeps the pages the scout chose.
_TOC_WORDS: dict[str, tuple[str, ...]] = {
    "pinout": (
        "pin assignment", "pin assignments", "pinout", "pin out",
        "pin description", "pin descriptions", "pin definition",
        "pin configuration", "pin function",
    ),
    "mcu": ("block diagram", "features", "product overview"),
    "regulator": ("power supply", "power management", "regulator"),
    "crystal": ("crystal", "clock", "oscillator"),
}


# A contents line: a title, then leaders (dots, spaced dots, or just a run of
# spaces), then the page it starts on. Requiring a title is what keeps the
# footer's bare page number out.
_TOC_LINE = re.compile(r"^\s{0,12}(?P<title>\S.*?\S)[\s.]{4,}(?P<page>\d{1,4})\s*$")


def toc_entries(pdf: Path, *, scan_pages: int = 12) -> list[tuple[str, int]]:
    """(title, page) for every contents line, in the order printed.

    The document already says where each section starts and, by naming the next
    one, where it ends. That is a statement, not an inference — better than
    matching repeated table headers, and it works for sections that are prose.

    The offset between a printed page number and a PDF page index is not assumed
    here; ``section_span`` resolves it by looking.
    """
    text = page_text(pdf, range(1, scan_pages + 1))
    out: list[tuple[str, int]] = []
    for line in text.splitlines():
        m = _TOC_LINE.match(line)
        if not m:
            continue
        title = " ".join(m.group("title").split()).rstrip(". ")
        page = int(m.group("page"))
        if title and 0 < page < 10_000:
            out.append((title, page))
    return out


def section_span(pdf: Path, *words: str, scan_pages: int = 12) -> list[int]:
    """The printed page range of the first contents entry matching ``words``.

    Empty when the document has no contents, or none of its entries match — in
    which case the caller keeps whatever the scout gave it.
    """
    entries = toc_entries(pdf, scan_pages=scan_pages)
    if not entries:
        return []
    wanted = [w.lower() for w in words]

    # Ranked, not first-match. "Pin configuration" occurs six times in the
    # nRF9151 contents as GPIO and peripheral *register* subsections, none of
    # which is the pinout; taking the first sent the extractor to page 163 for a
    # table that is on page 518. The entry that wins is the one where the phrase
    # is most nearly the whole heading, and where the phrase itself is the more
    # specific one — "pin assignments" beats "pin configuration".
    best: tuple[float, int, int] | None = None
    for i, (title, page) in enumerate(entries):
        # The section number is not part of the name.
        name = re.sub(r"^[\d.]+\s*", "", title).strip().lower()
        for rank, word in enumerate(wanted):
            if word not in name:
                continue
            coverage = len(word) / max(len(name), 1)
            score = (-rank, coverage)
            if best is None or score > (-best[0], best[1]):
                best = (rank, coverage, i)
            break
    if best is None:
        return []

    i = best[2]
    page = entries[i][1]
    # The next entry that starts on a *later* page ends this one. Sub-entries
    # sharing a page do not.
    end = next((p for _, p in entries[i + 1:] if p > page), page)
    return list(range(page, max(page, end - 1) + 1))


def _table_header(pdf: Path, number: int) -> tuple[str, ...] | None:
    """The header row of the first real table on a page.

    Read from the table structure rather than from the text, and that is the
    whole of the fix. Judging continuation by "the first substantial line" found
    the *running header* instead — "Refer to the online version for up-to-date
    content", which is on all 36 pages — so one document continued forever and
    another not at all.

    A table's header row is a much better signal, because it is the thing a
    document actually repeats when a table carries on.
    """
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        with pdfplumber.open(str(pdf)) as doc:
            if number < 1 or number > len(doc.pages):
                return None
            page = doc.pages[number - 1]
            for strategy in _TABLE_STRATEGIES:
                try:
                    tables = page.extract_tables(strategy)
                except Exception:  # noqa: BLE001
                    continue
                for table in tables:
                    for row in table:
                        cells = tuple((c or "").strip() for c in row)
                        if sum(1 for c in cells if c) >= 2:
                            return cells
    except Exception:  # noqa: BLE001 — an unreadable page ends the walk
        return None
    return None


def continued_pages(pdf: Path, pages: Sequence[int], *, limit: int = 30) -> list[int]:
    """The task's pages, plus any that carry the same table onward.

    The scout names where a section *starts*. A pin table does not politely end
    there: the nRF54L15 module's runs from page 12 to page 16, repeating
    ``Pin No. | Name | Pin function | Description`` at the top of each, and
    stopping at 17 where a different table begins. Reading only the named page
    returns a third of the pinout and reports success — the failure that is
    hardest to notice, because everything it does return is correct.
    """
    wanted = sorted({int(p) for p in pages})
    if not wanted:
        return []
    header = _table_header(pdf, wanted[0])
    if not header:
        return wanted
    nxt = wanted[-1] + 1
    while len(wanted) < limit:
        if _table_header(pdf, nxt) != header:
            break
        wanted.append(nxt)
        nxt += 1
    return wanted


def has_text_layer(pdf: Path) -> bool:
    """Whether this document can be read rather than looked at."""
    return bool(page_text(pdf, ()))


def _pages_label(pages: list[int] | None) -> str:
    """"5, 6, 13-15" — the human-readable form the prompt placeholder expects."""
    if not pages:
        return "all pages"
    pages = sorted(set(pages))
    runs: list[str] = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        runs.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = p
    runs.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(runs)


def _fill(template: str, *, mpn: str, pdf_path: str, pages: str, schema_path: str) -> str:
    return (
        template.replace("{{MPN}}", mpn)
        .replace("{{PDF_PATH}}", pdf_path)
        .replace("{{PAGES}}", pages)
        .replace("{{SCHEMA_PATH}}", schema_path)
    )


def _schema_version(schema_path: Path) -> str:
    try:
        return str(json.loads(schema_path.read_text(encoding="utf-8")).get("x-schema-version", "1.0"))
    except (OSError, json.JSONDecodeError):
        return "1.0"


def _append_ledger(cache_dir: Path, record: dict) -> None:
    """Per-task cost, in the shape the contract documents. Best-effort: losing a
    ledger line must never fail an extraction that otherwise worked."""
    try:
        with (cache_dir / "_cost_ledger.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


async def _one_task(
    *,
    endpoint: Endpoint,
    prompt: str,
    pdf_bytes: str,
    filename: str,
    schema: dict,
    page_text: str = "",
    tables: str = "",
) -> tuple[dict | None, str, int, int]:
    """Run one extractor. Returns (data, error, tokens_in, tokens_out).

    ``page_text`` is the document's own text, laid out, when it has one. When it
    is present the PDF is not sent at all: the model is asked to read text, not
    to look at a page.

    That is the whole difference between this working and not. Measured on the
    same seven datasheets: reading rendered pages produced 0 of 7 pinouts, all
    of them malformed JSON at near-identical byte offsets. Reading the text
    layer produced 113 of 113 pins on the nRF9151, correct including the ones an
    OCR pass merged. The text was there the entire time — every one of the seven
    had a text layer — and it is *right*, where a recogniser guessing at it gave
    "Digital l/O" for "Digital I/O" and "AINO" for "AIN0".

    So the PDF-as-image path is now the fallback, for documents that genuinely
    are scans.
    """
    adapter = build_chat_adapter(endpoint)
    instruction = (
        f"{prompt}\n\n"
        "Return ONLY the JSON value the schema below describes — an array if it "
        "says array, an object if it says object. No prose before or after it, "
        "no markdown fence, and nothing following it. It must validate against "
        "that schema, which is reproduced here so you do not need to open it:"
        "\n\n"
        f"{json.dumps(schema, indent=2)[:20000]}"
    )
    if page_text:
        body = ""
        if tables:
            body += (
                "The tables on these pages, read out of the PDF's own cell structure. "
                "Each row is one row of the table and `|` separates its cells; ` ⏎ ` "
                "inside a cell is a line break *within that cell*, which is how a pin "
                "with two functions is written — both belong to the same row.\n\n"
                "Use these for anything tabular. They are the document's own grid rather "
                "than an inference about where the columns were.\n\n"
                f"{tables}\n\n"
            )
        body += (
            "The same pages as running text, with the column layout preserved by "
            "whitespace — for prose, footnotes and anything the tables above missed.\n\n"
            f"<<<{filename}>>>\n{page_text}\n<<<end>>>\n\n"
        )
        content = [TextBlock(body + instruction)]
    else:
        content = [DocumentBlock(data=pdf_bytes, filename=filename), TextBlock(instruction)]
    messages = [Msg(role="user", content=content)]
    usage_in = usage_out = 0
    text_parts: list[str] = []
    want = limits.output_limit(endpoint, MAX_OUTPUT_TOKENS)

    async def attempt(cap: int) -> None:
        nonlocal usage_in, usage_out
        # The schema goes to the provider, not only into the prompt. Where the
        # server supports constrained decoding it masks the sampler so a token
        # that would break the schema cannot be chosen — which turns "the model
        # forgot a comma" from a thing that happens into a thing that cannot.
        # Servers that do not support it ignore the field.
        async for event in adapter.stream_chat(messages, max_tokens=cap, json_schema=schema):
            if isinstance(event, llm_chat.Usage):
                usage_in += event.input_tokens
                usage_out += event.output_tokens
            elif isinstance(event, llm_chat.Done):
                text_parts.append(event.message.text)

    try:
        await attempt(want)
    except Exception as exc:  # noqa: BLE001 — provider/transport failure is a task failure
        # A provider refusing the ceiling names the real one. That is the
        # authoritative figure from the only party that knows it, so it is
        # remembered and the call retried rather than reported as a failure —
        # the first call to an unfamiliar model should not have to be a
        # sacrifice.
        learned = limits.learn_from_error(endpoint.name, exc)
        if learned is None or learned >= want:
            return None, f"{type(exc).__name__}: {exc}", usage_in, usage_out
        text_parts.clear()
        try:
            await attempt(learned)
        except Exception as retry_exc:  # noqa: BLE001
            return None, f"{type(retry_exc).__name__}: {retry_exc}", usage_in, usage_out

    raw = "\n".join(t for t in text_parts if t).strip()
    if not raw:
        return None, "model returned no text", usage_in, usage_out
    payload, err = first_json_value(raw)

    # A refusal teaches the ceiling downward; running into it teaches upward.
    # Without this second half, a model whose real limit is higher than the
    # table's opening bid would be truncated forever and never say why —
    # nothing refuses, so nothing is learned, and a half-written pinout comes
    # back looking like a model that cannot finish a thought.
    if err and "cut off" in err and want < MAX_OUTPUT_TOKENS:
        bigger = min(want * 2, MAX_OUTPUT_TOKENS)
        text_parts.clear()
        try:
            await attempt(bigger)
        except Exception as exc:  # noqa: BLE001
            learned = limits.learn_from_error(endpoint.name, exc)
            if learned is not None:
                # It said no and named the real number, which is worth having
                # even though this attempt is lost.
                return None, f"{err} (retried at {bigger}, provider caps at {learned})", \
                    usage_in, usage_out
            return None, err, usage_in, usage_out
        raw = "\n".join(t for t in text_parts if t).strip()
        payload, err = first_json_value(raw)
        if not err:
            limits.learn(endpoint.name, bigger)

    if err:
        return None, err, usage_in, usage_out
    return payload, "", usage_in, usage_out


# How long an extraction answer may be.
#
# The shared default is 16,384, which is fine for prose and much too small
# here. A correct pinout for the nRF9151 — 113 pins, each with type, power
# domain, alternate functions and evidence — measured **21,292 output tokens**.
# It could not have fitted, so every pinout for a real part was cut off at
# exactly the ceiling and came back as broken JSON.
#
# That failure is indistinguishable from a model that cannot hold the schema
# unless something checks, which is why `first_json_value` reports
# "unterminated" separately: the fix for one is a bigger ceiling and for the
# other a different model, and confusing the two sent this work through a
# vision model, MinerU and an OCR pipeline before anybody looked at
# finish_reason.
#
# What extraction would *like*. What it actually asks any given endpoint for is
# this capped by that endpoint's real limit — declared, discovered from the
# server, or learned from the provider's own refusal. See blpl/core/limits.py.
#
# 64k rather than a snug fit. A 300-ball BGA has three times the pins of the
# part that set this number, and the cost of asking for headroom nobody uses is
# nothing — providers bill generated tokens, not the ceiling.
MAX_OUTPUT_TOKENS = int(os.environ.get("BLPL_EXTRACT_MAX_TOKENS") or 65536)


def first_json_value(raw: str) -> tuple[Any | None, str]:
    """The first complete JSON value in a model's reply.

    Scanned with a depth counter that understands strings and escapes, rather
    than sliced between the first ``{`` and the last ``}``. That slice was the
    single largest source of extraction failures, and it failed in a way that
    looked exactly like a model problem:

        100058045.pinout   malformed JSON: Extra data: line 20 column 4 (char 450)
        MAYA-W463.pinout   malformed JSON: Extra data: line 20 column 4 (char 450)
        MM8108.pinout      malformed JSON: Extra data: line 20 column 4 (char 450)
        NORA-B206.pinout   malformed JSON: Extra data: line 15 column 4 (char 420)

    Near-identical offsets across four unrelated documents, which reads as a
    model that breaks down at a fixed point. It is not. **The pinout schema is a
    top-level array.** A correct reply starts ``[`` — so ``find("{")`` landed on
    the first pin *inside* it, ``rfind("}")`` on the last, and json.loads parsed
    one pin object and then found the rest of the array sitting after it. The
    offsets matched because the first pin object is about the same size in every
    datasheet.

    Every pinout task in the corpus failed this way, on every model, including
    ones that had produced perfectly good output. The harness was breaking it.
    """
    text = raw.strip()
    # Fenced blocks, with or without a language tag.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = min(
        (i for i in (text.find("{"), text.find("[")) if i >= 0),
        default=-1,
    )
    if start < 0:
        return None, f"no JSON in output: {text[:200]}"
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1]), ""
                except json.JSONDecodeError as exc:
                    return None, f"malformed JSON: {exc}"
    return None, (
        f"output was cut off mid-value after {len(text)} characters — the answer "
        f"is longer than the {MAX_OUTPUT_TOKENS}-token ceiling. Raise "
        "BLPL_EXTRACT_MAX_TOKENS, or split the task across fewer pages. This is "
        "not a model that cannot hold the schema; it is one that was interrupted."
    )


def _validate(data: dict, schema_path: Path) -> str:
    """Schema-validate a result. Returns "" when valid, else the reason.

    The contract is explicit that ``status: "complete"`` *implies* schema-valid,
    so this gate is what makes a complete result trustworthy downstream.
    """
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError:
        return ""  # validation unavailable; merge_results.py will catch it
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"schema unreadable: {exc}"

    # Sibling schemas reference each other by filename, so resolve against the
    # directory the schema lives in.
    registry = Registry()
    for sibling in schema_path.parent.glob("*.schema.json"):
        try:
            registry = registry.with_resource(
                sibling.name, Resource.from_contents(json.loads(sibling.read_text(encoding="utf-8")))
            )
        except Exception:  # noqa: BLE001 — a bad sibling should not block validation
            continue
    try:
        jsonschema.Draft202012Validator(schema, registry=registry).validate(data)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        return f"schema validation failed at {path}: {exc.message}"
    except Exception as exc:  # noqa: BLE001
        return f"schema validation error: {exc}"
    return ""


async def extract_datasheet(
    mpn: str,
    pdf_path: Path,
    cache_dir: Path,
    endpoint: Endpoint | Sequence[Endpoint],
    *,
    force: bool = False,
    retry_failed: bool = False,
    max_parallel: int = 4,
    on_progress=None,
) -> ExtractionRun:
    """Scout, plan, dispatch, merge — the whole extraction for one part.

    ``endpoint`` may be a chain, and when it is, each task walks it until one
    model returns output that validates. This is not a retry for flakiness. A
    model either holds a JSON schema under a long PDF or it does not, and when
    it does not it fails the same way every time: a 7B vision model here
    produced malformed JSON at the same byte offset on seven different
    datasheets, emitted ``mA`` where the schema demanded base units, and
    ``ceramic`` where it demanded a dielectric class. Asking it again is
    pointless; asking a different model is the whole answer, and there was no
    way to express that — a task got one endpoint and one attempt.
    """
    chain: tuple[Endpoint, ...] = (
        (endpoint,) if isinstance(endpoint, Endpoint) else tuple(endpoint)
    )
    if not chain:
        run = ExtractionRun(mpn=mpn)
        run.error = "no vision endpoint supplied"
        return run
    endpoint = chain[0]
    run = ExtractionRun(mpn=mpn)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        run.error = f"no PDF at {pdf_path}"
        return run

    base = find_kicad_happy()
    if base is None:
        run.error = "kicad-happy not found — set BLPL_KICAD_HAPPY or init the submodule"
        return run
    schemas = base / "skills" / "datasheets" / "schemas"
    prompts = base / "skills" / "datasheets" / "prompts"

    pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]

    # Read the document rather than look at it, whenever it will let us. All
    # seven datasheets in the corpus that prompted this had a text layer; not
    # one of them needed a model that could see, and asking one to look at
    # rendered pages is what produced 0 of 7 pinouts.
    readable = has_text_layer(pdf_path)

    def note(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    # -- 1. scout ------------------------------------------------------------
    scout_file = cache_dir / f"{mpn}.scout.json"
    if not scout_file.exists() or force:
        note(f"{mpn}: scouting datasheet structure")
        prompt = _fill(
            (prompts / "scout.md").read_text(encoding="utf-8"),
            mpn=mpn,
            pdf_path=str(pdf_path),
            pages="all pages",
            schema_path=str(schemas / "scout.schema.json"),
        )
        scout_schema = json.loads((schemas / "scout.schema.json").read_text(encoding="utf-8"))
        data = None
        err = ""
        # Scouting is about structure — which section is where — so the front
        # matter and contents pages carry it, and the whole document does not
        # need to travel.
        scout_text = page_text(pdf_path, range(1, 41)) if readable else ""
        if readable:
            note(f"{mpn}: reading the document's own text (no vision needed)")
        for i, ep in enumerate(chain):
            data, err, tin, tout = await _one_task(
                endpoint=ep,
                prompt=prompt,
                pdf_bytes=pdf_b64,
                filename=pdf_path.name,
                schema=scout_schema,
                page_text=scout_text,
            )
            _append_ledger(
                cache_dir,
                {
                    "run_id": run_id, "mpn": mpn, "task_id": "scout", "tier": "B",
                    "model_id": ep.model, "tokens_in": tin, "tokens_out": tout,
                    "success": data is not None, "extracted_at": _now(),
                },
            )
            if data is not None:
                break
            if i + 1 < len(chain):
                note(f"{mpn}: scout failed on {ep.name} ({err}) — trying {chain[i + 1].name}")
        if data is None:
            run.error = f"scout failed: {err}"
            return run
        scout_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # -- 2. plan (kicad-happy owns this) -------------------------------------
    note(f"{mpn}: planning extraction tasks")
    plan_res = run_script(
        script_path("datasheets", "plan_extraction.py"),
        [mpn, str(pdf_path), "--cache-dir", str(cache_dir), "--use-cached-scout"]
        + (["--force"] if force else []),
        parse_json=False,
    )
    plan_file = cache_dir / f"{mpn}.plan.json"
    if not plan_file.exists():
        run.error = f"plan_extraction.py produced no plan: {plan_res.error}"
        return run
    plan = json.loads(plan_file.read_text(encoding="utf-8"))

    # -- 3. dispatch ---------------------------------------------------------
    # depends_on is honoured by construction: only tasks whose dependencies are
    # already complete are dispatched in a wave, and a wave that adds nothing
    # ends the loop rather than spinning.
    tasks = list(plan.get("tasks") or [])
    done: set[str] = set()
    semaphore = asyncio.Semaphore(max_parallel)

    for task in tasks:
        rf = cache_dir / f"{mpn}.{task['task_id']}.result.json"
        if rf.exists():
            try:
                existing = json.loads(rf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if existing.get("status") == "complete" and not force:
                done.add(task["task_id"])
                run.results.append(
                    TaskResult(task_id=task["task_id"], status="skipped", error="already complete")
                )

    async def dispatch(task: dict) -> TaskResult:
        task_id = task["task_id"]
        schema_path = Path(task["schema"])
        if not schema_path.is_absolute():
            schema_path = schemas / schema_path.name
        prompt_path = Path(task["prompt_template"])
        if not prompt_path.is_absolute():
            prompt_path = prompts / prompt_path.name

        pages = list(task.get("pages") or [])[:_MAX_PAGES_PER_TASK]
        prompt = _fill(
            prompt_path.read_text(encoding="utf-8"),
            mpn=mpn,
            pdf_path=str(pdf_path),
            pages=_pages_label(pages),
            schema_path=str(schema_path),
        )
        if retry_failed:
            prior = cache_dir / f"{mpn}.{task_id}.result.json"
            if prior.exists():
                try:
                    why = json.loads(prior.read_text(encoding="utf-8")).get("error", "")
                except json.JSONDecodeError:
                    why = ""
                if why:
                    prompt += (
                        f"\n\nYour previous output failed validation: {why}. "
                        "Correct it and try again."
                    )

        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        data: Any = None
        err = ""
        tin = tout = 0
        used = chain[0]
        # Only the pages this task is about. The plan already says which they
        # are, and a pin table read from its own six pages costs a few thousand
        # tokens where the whole 541-page document costs hundreds of thousands.
        # Where this section is, in the document's own words.
        #
        # A datasheet's contents page names the page each section starts on and,
        # by naming the next one, where it ends. That is a statement rather than
        # an inference, and it is what the scout's single page number was
        # standing in for badly: the nRF54L15 module's pin assignment runs 12 to
        # 16, so reading page 12 alone returned a third of the pinout and called
        # it done — the failure that is hardest to see, because everything it
        # does return is correct.
        span = list(pages)
        if readable:
            named = section_span(pdf_path, *_TOC_WORDS.get(task_id, ()))
            if named:
                span = named
                note(f"{mpn}: {task_id} is pages {named[0]}–{named[-1]} per the contents")
            else:
                # No contents entry: follow the repeated table header instead.
                span = continued_pages(pdf_path, pages)
                if len(span) > len(pages):
                    note(f"{mpn}: {task_id} continues past page {max(pages)} — {len(span)} pages")
        task_text = page_text(pdf_path, span) if readable else ""
        task_tables = page_tables(pdf_path, span) if readable else ""
        if task_text:
            # Measured against the window of the model that will read it, rather
            # than sent and hoped for. A slice that does not fit comes back as a
            # provider error or, worse, as a silently truncated read — and the
            # cost of knowing in advance is a character count.
            room = max(4_000, int(budget_for(chain[0]) * 0.6))
            estimate = len(task_text) // 4
            if estimate > room:
                keep_pages = max(1, int(len(span or [1]) * room / estimate))
                note(
                    f"{mpn}: {task_id} spans ~{estimate:,} tokens, over the "
                    f"~{room:,} that fits — reading the first {keep_pages} of "
                    f"{len(span)} pages"
                )
                task_text = page_text(pdf_path, sorted(span)[:keep_pages])
                task_tables = page_tables(pdf_path, sorted(span)[:keep_pages])
        async with semaphore:
            for i, ep in enumerate(chain):
                note(
                    f"{mpn}: extracting {task_id}"
                    + (f" (attempt {i + 1}, {ep.name})" if i else "")
                )
                data, err, ti, to = await _one_task(
                    endpoint=ep,
                    prompt=prompt,
                    pdf_bytes=pdf_b64,
                    filename=pdf_path.name,
                    schema=schema,
                    page_text=task_text,
                    tables=task_tables,
                )
                if data is not None and not err:
                    err = _validate(data, schema_path)
                    if err:
                        # Show it what it got wrong and let it correct itself,
                        # once, before writing the attempt off.
                        #
                        # Nearly all of these are one wrong word in an otherwise
                        # complete answer: a 45-pin extraction with every pin
                        # right and `"type": "analog_in"` where the enum has no
                        # analog member, or "ceramic" where a dielectric class
                        # was wanted, or "mA" where the unit had to be a base
                        # one. Throwing away 45 correct pins over a word — and
                        # then blaming the model — is what made this look like a
                        # model problem for so long.
                        #
                        # This is the fallback for constrained decoding, not a
                        # substitute for it. The schema is sent as
                        # response_format on every call; where a server honours
                        # it the sampler cannot produce these at all. This
                        # deployment does not honour it — a custom reasoning
                        # parser and structured outputs do not appear to
                        # cooperate — so the correction has to happen after the
                        # fact.
                        fixed, again, ti2, to2 = await _one_task(
                            endpoint=ep,
                            prompt=(
                                f"{prompt}\n\nYour previous answer was complete but did not "
                                f"validate:\n\n    {err}\n\nReturn the whole thing again with "
                                "only that corrected. Use a value the schema's enum actually "
                                "lists; do not invent one, and do not drop the entry."
                            ),
                            pdf_bytes=pdf_b64,
                            filename=pdf_path.name,
                            schema=schema,
                            page_text=task_text,
                            tables=task_tables,
                        )
                        ti += ti2
                        to += to2
                        if fixed is not None and not again and not _validate(fixed, schema_path):
                            note(f"{mpn}: {task_id} corrected a schema slip and validated")
                            data, err = fixed, ""
                        else:
                            data = None
                used, tin, tout = ep, ti, to
                # Billed per attempt, so recorded per attempt: a ledger that
                # only notes the model that finally worked understates what the
                # run cost and hides which model is burning the budget.
                _append_ledger(
                    cache_dir,
                    {
                        "run_id": run_id, "mpn": mpn, "task_id": task_id,
                        "tier": task.get("tier", "B"), "model_id": ep.model,
                        "tokens_in": ti, "tokens_out": to,
                        "success": data is not None, "extracted_at": _now(),
                    },
                )
                if data is not None:
                    break
                if i + 1 < len(chain):
                    note(f"{mpn}: {task_id} failed on {ep.name} ({err}) — trying {chain[i + 1].name}")

        wrapped = {
            "task_id": task_id,
            "schema_version": _schema_version(schema_path),
            "status": "complete" if data is not None else "failed",
            "extracted_at": _now(),
            "model_tier": task.get("tier", "B"),
            "model_id": used.model,
            "data": data,
        }
        if data is None:
            wrapped["error"] = err or "unknown extraction failure"
        (cache_dir / f"{mpn}.{task_id}.result.json").write_text(
            json.dumps(wrapped, indent=2), encoding="utf-8"
        )
        return TaskResult(
            task_id=task_id,
            status=wrapped["status"],
            error=wrapped.get("error", ""),
            model_id=used.model,
            tokens_in=tin,
            tokens_out=tout,
        )

    pending = [t for t in tasks if t["task_id"] not in done]
    while pending:
        wave = [t for t in pending if all(d in done for d in (t.get("depends_on") or []))]
        if not wave:
            for t in pending:
                run.results.append(
                    TaskResult(
                        task_id=t["task_id"],
                        status="failed",
                        error=f"dependencies never completed: {t.get('depends_on')}",
                    )
                )
            break
        for result in await asyncio.gather(*(dispatch(t) for t in wave)):
            run.results.append(result)
            if result.status == "complete":
                done.add(result.task_id)
        pending = [t for t in pending if t["task_id"] not in {r.task_id for r in run.results}]

    if not run.results:
        # The planner found nothing to do. Almost always the scout declining the
        # document, and its reason is the useful part — so it is carried out
        # rather than left in a cache file nobody opens.
        verdict = {}
        try:
            verdict = json.loads(scout_file.read_text(encoding="utf-8")).get("quality_verdict") or {}
        except (OSError, json.JSONDecodeError):
            pass
        why = verdict.get("reason") or "the planner produced no tasks"
        run.error = f"nothing to extract from {pdf_path.name}: {why}"
        return run

    # -- 4. merge (kicad-happy owns this) ------------------------------------
    #
    # merge_results.py is a two-pass design and BLPL only ever ran the first
    # pass. Without --retry-failed it refuses on the first failed task and
    # writes nothing, expecting the caller to re-dispatch and come back; with
    # it, the tasks that did succeed are spliced in and the ones that did not
    # get an {"_extraction_failed": true} sentinel in their place.
    #
    # Running only the strict pass made the whole extraction all-or-nothing:
    # one failed pinout discarded the mcu section that had extracted cleanly
    # beside it, on the same part, in the same run. The re-dispatch that pass
    # was waiting for has now happened — every task has already walked the
    # endpoint chain above — so what is left is to keep what survived.
    note(f"{mpn}: merging results")
    args = [mpn, "--cache-dir", str(cache_dir)]
    merge = run_script(
        script_path("datasheets", "merge_results.py"),
        args + (["--retry-failed"] if retry_failed else []),
        parse_json=False,
    )
    if not merge.ok and not retry_failed and any(r.status == "failed" for r in run.results):
        note(f"{mpn}: some tasks failed — merging what succeeded")
        merge = run_script(
            script_path("datasheets", "merge_results.py"),
            args + ["--retry-failed"],
            parse_json=False,
        )
    if not merge.ok and not (cache_dir / f"{mpn}.json").exists():
        run.error = f"merge_results.py failed: {merge.error}"
    return run
