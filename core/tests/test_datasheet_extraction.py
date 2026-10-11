"""Extraction across a chain of endpoints, and what survives a partial failure.

Both behaviours here exist because of the same run: a 7B vision model produced
malformed JSON at near-identical byte offsets on seven different datasheets,
emitted ``mA`` where the schema demanded base units, and ``ceramic`` where it
demanded a dielectric class. That is a model failing to hold an output
contract, not a flaky call — so retrying it is pointless and asking a different
model is the entire fix. The old dispatch could express neither: one endpoint,
one attempt, and one failed task discarded every task that had succeeded
alongside it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from blpl.agent.tools import datasheets
from blpl.core.llm_chat import Endpoint


def _endpoint(name: str) -> Endpoint:
    return Endpoint(name=name, kind="anthropic", model=f"{name}-model", api_key="k")


@pytest.fixture
def happy(tmp_path: Path, monkeypatch) -> Path:
    """A stand-in kicad-happy: real schema and prompt files, fake scripts."""
    base = tmp_path / "kicad-happy"
    schemas = base / "skills" / "datasheets" / "schemas"
    prompts = base / "skills" / "datasheets" / "prompts"
    schemas.mkdir(parents=True)
    prompts.mkdir(parents=True)
    (schemas / "scout.schema.json").write_text('{"type": "object"}', encoding="utf-8")
    # The one that matters: 'family' required is exactly the violation the real
    # run hit on four of seven parts.
    (schemas / "mcu.schema.json").write_text(
        json.dumps({"type": "object", "required": ["family"]}), encoding="utf-8"
    )
    (prompts / "scout.md").write_text("scout {{MPN}}", encoding="utf-8")
    (prompts / "mcu.md").write_text("mcu {{MPN}}", encoding="utf-8")
    monkeypatch.setattr(datasheets, "find_kicad_happy", lambda: base)
    monkeypatch.setattr(datasheets, "script_path", lambda *a: base / "script.py")
    return base


@pytest.fixture
def scripted(happy, tmp_path: Path, monkeypatch):
    """Fake plan_extraction.py and merge_results.py, recording their arguments."""
    calls: list[list[str]] = []

    def run_script(path, args, **kw):
        calls.append(list(args))
        cache = Path(args[args.index("--cache-dir") + 1])
        mpn = args[0]
        if not (cache / f"{mpn}.plan.json").exists():
            (cache / f"{mpn}.plan.json").write_text(
                json.dumps(
                    {"tasks": [{"task_id": "mcu", "schema": "mcu.schema.json",
                                "prompt_template": "mcu.md", "pages": [1], "tier": "B"}]}
                ),
                encoding="utf-8",
            )
            return _Result(True, "")
        # merge: refuse unless told to keep partial results
        ok = "--retry-failed" in args or not _any_failed(cache, mpn)
        if ok:
            (cache / f"{mpn}.json").write_text("{}", encoding="utf-8")
        return _Result(ok, "" if ok else "task mcu failed")

    monkeypatch.setattr(datasheets, "run_script", run_script)
    return calls


class _Result:
    def __init__(self, ok: bool, error: str) -> None:
        self.ok, self.error, self.data = ok, error, None


def _any_failed(cache: Path, mpn: str) -> bool:
    return any(
        json.loads(f.read_text(encoding="utf-8")).get("status") == "failed"
        for f in cache.glob(f"{mpn}.*.result.json")
    )


def _pdf(tmp_path: Path) -> Path:
    p = tmp_path / "part.pdf"
    p.write_bytes(b"%PDF-1.4 pretend")
    return p


def _run(tmp_path, chain, answers, monkeypatch):
    """Drive one extraction with ``answers`` keyed by endpoint name."""
    seen: list[str] = []

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text="", tables=""):
        seen.append(endpoint.name)
        return (answers.get(endpoint.name), "" if answers.get(endpoint.name) else "boom", 10, 5)

    monkeypatch.setattr(datasheets, "_one_task", one_task)
    run = asyncio.run(
        datasheets.extract_datasheet(
            "PART1", _pdf(tmp_path), tmp_path / "cache", chain
        )
    )
    return run, seen


def test_a_task_walks_the_chain_when_the_first_model_cannot_hold_the_schema(
    tmp_path, scripted, monkeypatch
) -> None:
    """The failure is schema-invalid output, not an error — output that parses
    and then does not validate is the signature of a model too small for the
    contract, and it is the case a single-endpoint dispatch could never escape."""
    chain = [_endpoint("small"), _endpoint("big")]
    run, seen = _run(
        tmp_path,
        chain,
        # 'small' returns something well-formed and schema-invalid: no 'family'.
        {"small": {"pins": []}, "big": {"family": "STM32U5"}},
        monkeypatch,
    )
    # Scout accepts anything, so 'small' serves it; the mcu task is where the
    # schema bites and where the chain is walked.
    assert seen.count("big") >= 1
    assert [r.status for r in run.results] == ["complete"]
    assert run.results[0].model_id == "big-model"


def test_a_chain_of_one_does_not_walk_to_another_model(tmp_path, scripted, monkeypatch) -> None:
    """No silent attempt at a *different* model, because there is not one. The
    same model does get one correction, which is a different thing: it is being
    told what was wrong rather than asked the same question again."""
    run, seen = _run(tmp_path, [_endpoint("small")], {"small": {"pins": []}}, monkeypatch)
    assert [r.status for r in run.results] == ["failed"]
    assert set(seen) == {"small"}
    assert len(seen) == 3  # scout, the attempt, one correction


def test_what_succeeded_survives_a_sibling_task_failing(
    tmp_path, scripted, monkeypatch
) -> None:
    """merge_results.py is a two-pass design and only the strict pass was ever
    run, so one failed task discarded every task that had worked beside it."""
    _run(tmp_path, [_endpoint("small")], {"small": {"pins": []}}, monkeypatch)
    merges = [c for c in scripted if "--cache-dir" in c and c[0] == "PART1"]
    assert any("--retry-failed" in c for c in merges), (
        "after a task failed, the merge must be re-run in the mode that keeps "
        "the results that succeeded"
    )
    assert (tmp_path / "cache" / "PART1.json").exists()


def test_no_endpoints_is_a_clear_refusal_not_a_crash(tmp_path, scripted) -> None:
    run = asyncio.run(
        datasheets.extract_datasheet("PART1", _pdf(tmp_path), tmp_path / "cache", [])
    )
    assert not run.ok and "no vision endpoint" in run.error


# -- reading rather than looking ---------------------------------------------


def test_a_document_with_a_text_layer_is_read_not_looked_at(tmp_path, scripted, monkeypatch) -> None:
    """The measurement that changed the whole approach: reading rendered pages
    produced 0 of 7 pinouts, all malformed JSON at near-identical byte offsets.
    Reading the text layer produced 113 of 113 pins on the nRF9151 — including
    the ones an OCR pass merged. Every one of the seven had a text layer."""
    seen: list[bool] = []

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text="", tables=""):
        seen.append(bool(page_text))
        return ({"family": "x"}, "", 10, 5)

    monkeypatch.setattr(datasheets, "_one_task", one_task)
    monkeypatch.setattr(datasheets, "has_text_layer", lambda p: True)
    monkeypatch.setattr(datasheets, "page_text", lambda p, pages=(): "1  VDD  Power  Supply")
    asyncio.run(
        datasheets.extract_datasheet("PART1", _pdf(tmp_path), tmp_path / "cache", [_endpoint("m")])
    )
    assert seen and all(seen), "every task should have been given the document's own text"


def test_a_scan_still_goes_as_pages(tmp_path, scripted, monkeypatch) -> None:
    """The image path is the fallback, not the default — but it is still there,
    because a scanned datasheet is a real thing and has no text to read."""
    seen: list[bool] = []

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text="", tables=""):
        seen.append(bool(page_text))
        return ({"family": "x"}, "", 10, 5)

    monkeypatch.setattr(datasheets, "_one_task", one_task)
    monkeypatch.setattr(datasheets, "has_text_layer", lambda p: False)
    asyncio.run(
        datasheets.extract_datasheet("PART1", _pdf(tmp_path), tmp_path / "cache", [_endpoint("m")])
    )
    assert seen and not any(seen)


def test_a_page_of_form_feed_is_not_a_text_layer(tmp_path) -> None:
    """A scanned page yields a form feed and nothing else. Judging on length
    rather than printable characters would make a 900-page scan look like a
    document with plenty of text."""
    import subprocess as sp

    class Fake:
        stdout = "\f" * 40

    orig = datasheets.subprocess.run
    datasheets.subprocess.run = lambda *a, **k: Fake()
    try:
        assert datasheets.page_text(tmp_path / "x.pdf", range(1, 41)) == ""
    finally:
        datasheets.subprocess.run = orig
    assert sp is not None


def test_only_the_pages_a_task_is_about_are_sent(tmp_path, scripted, monkeypatch) -> None:
    """A pin table read from its own six pages costs a few thousand tokens where
    the whole 541-page document costs hundreds of thousands."""
    asked: list[object] = []

    def fake_page_text(pdf, pages=()):
        asked.append(list(pages) if pages else [])
        return "1  VDD  Power  Supply"

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text="", tables=""):
        return ({"family": "x"}, "", 10, 5)

    monkeypatch.setattr(datasheets, "_one_task", one_task)
    monkeypatch.setattr(datasheets, "has_text_layer", lambda p: True)
    monkeypatch.setattr(datasheets, "page_text", fake_page_text)
    asyncio.run(
        datasheets.extract_datasheet("PART1", _pdf(tmp_path), tmp_path / "cache", [_endpoint("m")])
    )
    # The plan gives the mcu task page 1; the scout gets the front matter. The
    # point is that neither asks for the whole document.
    assert [1] in asked


def test_a_run_that_extracted_nothing_is_not_ok(tmp_path, happy, monkeypatch) -> None:
    """A green tick over an empty result is worse than an error. An error gets
    read; a success gets believed.

    The case: the scout declines a document because the exact MPN string is not
    in it — the part is a module sold under a distributor SKU while the datasheet
    calls it by its product name — the planner emits zero tasks, the merge has
    nothing to object to, and the whole run came back ok.
    """
    calls: list[list[str]] = []

    def run_script(path, args, **kw):
        calls.append(list(args))
        cache = Path(args[args.index("--cache-dir") + 1])
        mpn = args[0]
        if not (cache / f"{mpn}.plan.json").exists():
            (cache / f"{mpn}.plan.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
        return _Result(True, "")

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text="", tables=""):
        return ({"quality_verdict": {"verdict": "skip", "reason": "target MPN not found in PDF"}},
                "", 10, 5)

    monkeypatch.setattr(datasheets, "run_script", run_script)
    monkeypatch.setattr(datasheets, "_one_task", one_task)
    monkeypatch.setattr(datasheets, "has_text_layer", lambda p: True)
    monkeypatch.setattr(datasheets, "page_text", lambda p, pages=(): "text")
    run = asyncio.run(
        datasheets.extract_datasheet("PART1", _pdf(tmp_path), tmp_path / "cache", [_endpoint("m")])
    )
    assert not run.ok
    # And the scout's reason is carried out, not left in a cache file nobody opens.
    assert "target MPN not found in PDF" in run.error


# -- reading the model's reply ------------------------------------------------


def test_a_top_level_array_is_not_mistaken_for_its_first_element() -> None:
    """The single largest source of extraction failures, and it looked exactly
    like a model problem:

        100058045.pinout   malformed JSON: Extra data: line 20 column 4 (char 450)
        MAYA-W463.pinout   malformed JSON: Extra data: line 20 column 4 (char 450)
        MM8108.pinout      malformed JSON: Extra data: line 20 column 4 (char 450)

    Near-identical offsets across unrelated documents. The pinout schema is a
    top-level array: slicing between the first '{' and the last '}' took the
    first pin *object* and left the rest of the array after it. The offsets
    matched because the first pin is about the same size in every datasheet.
    """
    reply = '[{"numbers": ["1"], "name": "GND"}, {"numbers": ["2"], "name": "P0.20"}]'
    value, err = datasheets.first_json_value(reply)
    assert not err
    assert isinstance(value, list) and len(value) == 2


def test_prose_after_the_json_is_ignored() -> None:
    value, err = datasheets.first_json_value('{"a": 1}\n\nI hope that helps!')
    assert not err and value == {"a": 1}


def test_a_fenced_block_is_unwrapped() -> None:
    value, err = datasheets.first_json_value('```json\n{"a": 1}\n```')
    assert not err and value == {"a": 1}


def test_a_brace_inside_a_string_does_not_close_the_value() -> None:
    """The reason this is a scanner and not a bracket count."""
    value, err = datasheets.first_json_value('{"note": "a } inside", "b": 2}')
    assert not err and value == {"note": "a } inside", "b": 2}


def test_an_escaped_quote_does_not_end_the_string() -> None:
    value, err = datasheets.first_json_value(r'{"note": "he said \"hi\"", "b": 2}')
    assert not err and value["b"] == 2


def test_output_cut_off_mid_value_says_so() -> None:
    """Distinct from malformed: the fix is a bigger max_tokens, not a different
    model, and the message has to say which."""
    value, err = datasheets.first_json_value('[{"numbers": ["1"], "name": "GN')
    assert value is None and "cut off" in err


def test_reasoning_prose_before_the_json_is_skipped() -> None:
    value, err = datasheets.first_json_value(
        'Let me work through the table.\nThe first pin is GND.\n[{"name": "GND"}]'
    )
    assert not err and value == [{"name": "GND"}]


def test_the_output_ceiling_fits_a_real_pinout() -> None:
    """A correct pinout for the nRF9151 — 113 pins with type, power domain,
    alternate functions and evidence — measured 21,292 output tokens. The shared
    default is 16,384, so every pinout for a real part was cut off at exactly
    the ceiling and came back as broken JSON."""
    from blpl.core.llm_chat import DEFAULT_MAX_TOKENS

    assert datasheets.MAX_OUTPUT_TOKENS > 21_292
    assert datasheets.MAX_OUTPUT_TOKENS > DEFAULT_MAX_TOKENS


def test_truncation_is_reported_as_truncation_not_as_a_bad_model() -> None:
    """The fix for one is a bigger ceiling and for the other a different model.
    Confusing them sent this work through a vision model, MinerU and an OCR
    pipeline before anybody looked at finish_reason."""
    _, err = datasheets.first_json_value('[{"numbers": ["1"], "name": "GN')
    assert "cut off" in err and "ceiling" in err
    assert "not a model that cannot hold the schema" in err


def test_the_slice_is_sized_to_the_model_that_will_read_it(tmp_path, scripted, monkeypatch) -> None:
    """A 4B model on Ollama and a router's frontier model are three orders of
    magnitude apart. Sending a slice and hoping produces either a provider error
    or, worse, a silently truncated read."""
    asked: list[list[int]] = []

    def fake_page_text(pdf, pages=()):
        asked.append(sorted(pages) if pages else [])
        # Far more text than a small window could take.
        return "x" * 400_000

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text="", tables=""):
        return ({"family": "x"}, "", 10, 5)

    monkeypatch.setattr(datasheets, "_one_task", one_task)
    monkeypatch.setattr(datasheets, "has_text_layer", lambda p: True)
    monkeypatch.setattr(datasheets, "page_text", fake_page_text)
    monkeypatch.setattr(datasheets, "budget_for", lambda ep: 8_000)
    asyncio.run(
        datasheets.extract_datasheet("PART1", _pdf(tmp_path), tmp_path / "cache", [_endpoint("m")])
    )
    # It asked for the task's pages, found them too big, and asked again for
    # fewer — rather than sending 400k characters at an 8k budget.
    assert len(asked) >= 3


def test_a_window_is_asked_of_the_endpoint_not_assumed(monkeypatch) -> None:
    """The same model served two ways has two different windows: this network's
    Nemotron reports 512,000 where the model id alone would have said
    1,000,000."""
    from blpl.core import limits

    monkeypatch.setattr(limits, "describe", lambda *a: (512_000, None))
    ep = Endpoint(name="thor", kind="openai-compatible", model="nvidia/nemotron-3-super",
                  base_url="http://x/v1", api_key="k")
    room = datasheets.budget_for(ep)
    assert 400_000 < room < 512_000  # the window, less what the answer needs


# -- reading the grid the PDF already has ------------------------------------


def _grid_pdf(path: Path) -> Path:
    """A one-page PDF with a ruled 3x3 table, drawn the way a datasheet draws
    one: lines for the cell borders and text inside them."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    rows = [["Pin", "Name", "Type"], ["1", "GND", "Ground"], ["2", "ANT", "Analog"]]
    x0, y0, w, h = 60, 700, 120, 22
    for r, row in enumerate(rows):
        for col, cell in enumerate(row):
            x, y = x0 + col * w, y0 - r * h
            c.rect(x, y, w, h)
            c.drawString(x + 4, y + 6, cell)
    c.showPage()
    c.save()
    return path


def test_a_ruled_table_comes_back_as_cells(tmp_path) -> None:
    """The part that laying text out flat could never do. A pin table is a grid
    and the PDF says so — it has the ruling lines, the cell bounds and the runs
    inside them. Flattening it and asking a model to infer the grid back throws
    that away, and every trick after that is an attempt to recover what the file
    had already handed us."""
    pytest.importorskip("reportlab")
    pytest.importorskip("pdfplumber")
    pdf = _grid_pdf(tmp_path / "grid.pdf")
    out = datasheets.page_tables(pdf, [1])
    assert "Pin | Name | Type" in out
    assert "1 | GND | Ground" in out
    assert "2 | ANT | Analog" in out


def test_a_document_with_no_tables_says_nothing_rather_than_guessing(tmp_path) -> None:
    pytest.importorskip("reportlab")
    pytest.importorskip("pdfplumber")
    from reportlab.pdfgen import canvas

    p = tmp_path / "prose.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(70, 700, "Just a sentence, with no table anywhere near it.")
    c.showPage()
    c.save()
    # No ruling lines and one line of text: nothing that is honestly a table.
    assert "|" not in datasheets.page_tables(p, [1]).replace("[table", "")


def test_an_unreadable_file_falls_back_instead_of_failing(tmp_path) -> None:
    """Tables are an improvement on the text layer, never a precondition for it."""
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 this is not a pdf")
    assert datasheets.page_tables(broken, [1]) == ""


def test_a_cell_holding_two_lines_stays_one_row(tmp_path) -> None:
    """A pin with two functions is one cell containing two lines, which is
    exactly what it means. On the real nRF9151 page this is
    `2 | P0.20 ⏎ AIN7 | …` — where -layout gave two unlinked lines and OCR gave
    'TRACECLK P0.22' merged into one string."""
    assert datasheets._render_table([["2", "P0.20\nAIN7", "Digital I/O"]]) == (
        "2 | P0.20 ⏎ AIN7 | Digital I/O"
    )


def test_blank_rows_are_dropped(tmp_path) -> None:
    assert datasheets._render_table([["a", "b"], [None, ""], ["c", "d"]]) == "a | b\nc | d"


def test_a_table_is_followed_past_the_page_the_scout_named(tmp_path, monkeypatch) -> None:
    """The scout names where a section starts. A pin table does not politely end
    there — the nRF54L15 module's runs 12 → 16, repeating its header on each and
    stopping at 17 where a different table begins. Reading only the named page
    returns a third of the pinout and reports success, which is the failure that
    is hardest to notice because everything it does return is correct.
    """
    headers = {
        12: ("Pin No.", "Name", "Pin function", "Description"),
        13: ("Pin No.", "Name", "Pin function", "Description"),
        14: ("Pin No.", "Name", "Pin function", "Description"),
        15: ("Pin No.", "Name", "Pin function", "Description"),
        16: ("Pin No.", "Name", "Pin function", "Description"),
        17: ("RF IC", "Crystal Frequency"),
    }
    monkeypatch.setattr(datasheets, "_table_header", lambda pdf, n: headers.get(n))
    assert datasheets.continued_pages(Path("x.pdf"), [12]) == [12, 13, 14, 15, 16]


def test_a_one_page_table_stays_one_page(tmp_path, monkeypatch) -> None:
    """MM8108's pin table is 38 pins on page 8, and page 9 is footnotes."""
    headers = {8: ("Pin", "Pin Name", "Type"), 9: ("", "Refer to the onlin", "e version f")}
    monkeypatch.setattr(datasheets, "_table_header", lambda pdf, n: headers.get(n))
    assert datasheets.continued_pages(Path("x.pdf"), [8]) == [8]


def test_continuation_is_judged_on_the_table_not_on_page_furniture(monkeypatch) -> None:
    """Judging by "the first substantial line" found the *running header* —
    "Refer to the online version for up-to-date content", on all 36 pages — so
    one document continued forever and another not at all."""
    monkeypatch.setattr(datasheets, "_table_header", lambda pdf, n: None)
    # No table to compare, so it does not invent a continuation.
    assert datasheets.continued_pages(Path("x.pdf"), [8]) == [8]


def test_the_walk_is_bounded(monkeypatch) -> None:
    """A header that matches forever must not read a 940-page datasheet."""
    monkeypatch.setattr(datasheets, "_table_header", lambda pdf, n: ("same",  "header"))
    assert len(datasheets.continued_pages(Path("x.pdf"), [1], limit=5)) == 5


def test_pages_are_labelled_with_their_real_numbers() -> None:
    """pdftotext separates pages with a form feed and says nothing about which
    page is which. The schema asks every finding for its page, so without this
    the model is guessing at the one field that makes a claim checkable."""
    assert datasheets._mark_pages("first\fsecond", 12) == (
        "--- Page 12 ---\nfirst\n--- Page 13 ---\nsecond"
    )
    # Blank pages do not consume a number they never had.
    assert "Page 13" not in datasheets._mark_pages("first\f   \f", 12)


# -- letting the document say where its sections are -------------------------


def test_a_contents_line_is_read_as_title_and_page() -> None:
    """The document already says where each section starts and, by naming the
    next one, where it ends. That is a statement, not an inference."""
    line = "     2.5. Pin assignment ................................................... 12"
    m = datasheets._TOC_LINE.match(line)
    assert m and m.group("page") == "12"
    assert "Pin assignment" in m.group("title")


def test_spaced_dot_leaders_are_read_too() -> None:
    """Nordic's contents uses '. . . . .' with spaces between."""
    m = datasheets._TOC_LINE.match("        11.1.1 LGA pin assignments. . . . . . . . . . . . 518")
    assert m and m.group("page") == "518"


def test_a_bare_page_number_is_not_a_contents_line() -> None:
    """The footer prints one on every page. Requiring a title keeps it out."""
    assert datasheets._TOC_LINE.match("                          12") is None


def test_the_more_specific_heading_wins(monkeypatch) -> None:
    """'Pin configuration' occurs six times in the nRF9151 contents as GPIO and
    peripheral *register* subsections, none of which is the pinout. Taking the
    first match sent the extractor to page 163 for a table on page 518."""
    entries = [
        ("6.4.1 Pin configuration", 163),
        ("6.5.3 Tasks and events pin configuration", 173),
        ("11.1 Pin assignments", 518),
        ("11.2 Mechanical specifications", 521),
    ]
    monkeypatch.setattr(datasheets, "toc_entries", lambda pdf, scan_pages=12: entries)
    got = datasheets.section_span(Path("x.pdf"), "pin assignment", "pin configuration")
    assert got == [518, 519, 520]


def test_a_section_ends_where_the_next_one_starts(monkeypatch) -> None:
    entries = [("2.5. Pin assignment", 12), ("3. Main chip solution", 17)]
    monkeypatch.setattr(datasheets, "toc_entries", lambda pdf, scan_pages=12: entries)
    assert datasheets.section_span(Path("x.pdf"), "pin assignment") == [12, 13, 14, 15, 16]


def test_subentries_sharing_a_page_do_not_end_a_section(monkeypatch) -> None:
    entries = [("11.1 Pin assignments", 518), ("11.1.1 LGA pin assignments", 518),
               ("11.2 Mechanical", 521)]
    monkeypatch.setattr(datasheets, "toc_entries", lambda pdf, scan_pages=12: entries)
    assert datasheets.section_span(Path("x.pdf"), "pin assignment") == [518, 519, 520]


def test_no_contents_leaves_the_scout_alone(monkeypatch) -> None:
    monkeypatch.setattr(datasheets, "toc_entries", lambda pdf, scan_pages=12: [])
    assert datasheets.section_span(Path("x.pdf"), "pinout") == []


def test_a_schema_slip_is_corrected_rather_than_discarded(tmp_path, scripted, monkeypatch) -> None:
    """Nearly all of these are one wrong word in an otherwise complete answer: a
    45-pin extraction with every pin right and `"type": "analog_in"` where the
    enum has no analog member. Throwing away 45 correct pins over a word — and
    then blaming the model — is what made this look like a model problem for so
    long.
    """
    calls: list[str] = []

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text="", tables=""):
        calls.append(prompt)
        # The first *task* answer is complete and uses a value the enum does not
        # list; the correction, told exactly what was wrong, uses one it does.
        corrected = "did not validate" in prompt
        return ({"family": "x", "type": "input" if corrected else "analog_in"}, "", 10, 5)

    def validate(data, schema_path):
        return "" if data.get("type") == "input" else "'analog_in' is not one of ['input', …]"

    monkeypatch.setattr(datasheets, "_one_task", one_task)
    monkeypatch.setattr(datasheets, "_validate", validate)
    monkeypatch.setattr(datasheets, "has_text_layer", lambda p: True)
    monkeypatch.setattr(datasheets, "page_text", lambda p, pages=(): "text")
    monkeypatch.setattr(datasheets, "page_tables", lambda p, pages=(): "")
    monkeypatch.setattr(datasheets, "section_span", lambda p, *w, **k: [1])

    run = asyncio.run(
        datasheets.extract_datasheet("PART1", _pdf(tmp_path), tmp_path / "cache", [_endpoint("m")])
    )
    assert [r.status for r in run.results] == ["complete"]
    # It was told what was wrong, in the provider's own words.
    assert len(calls) == 3  # scout, the attempt, the correction
    assert "did not validate" in calls[2] and "analog_in" in calls[2]


def test_a_correction_that_fails_again_is_not_retried_forever(
    tmp_path, scripted, monkeypatch
) -> None:
    """One correction, not a loop. Output that is wrong in a way the model
    cannot see is wrong however many times it is asked."""
    calls: list[str] = []

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text="", tables=""):
        calls.append(prompt)
        return ({"family": "x"}, "", 10, 5)

    monkeypatch.setattr(datasheets, "_one_task", one_task)
    monkeypatch.setattr(datasheets, "_validate", lambda d, s: "still wrong")
    monkeypatch.setattr(datasheets, "has_text_layer", lambda p: True)
    monkeypatch.setattr(datasheets, "page_text", lambda p, pages=(): "text")
    monkeypatch.setattr(datasheets, "page_tables", lambda p, pages=(): "")
    monkeypatch.setattr(datasheets, "section_span", lambda p, *w, **k: [1])

    run = asyncio.run(
        datasheets.extract_datasheet("PART1", _pdf(tmp_path), tmp_path / "cache", [_endpoint("m")])
    )
    assert [r.status for r in run.results] == ["failed"]
    # Scout, the attempt, and one correction — not a third.
    assert len(calls) == 3
