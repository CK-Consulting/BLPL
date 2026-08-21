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

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text=""):
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


def test_a_chain_of_one_still_fails_exactly_as_before(tmp_path, scripted, monkeypatch) -> None:
    """No silent second attempt at the same model: it would spend twice and
    change nothing."""
    run, seen = _run(tmp_path, [_endpoint("small")], {"small": {"pins": []}}, monkeypatch)
    assert [r.status for r in run.results] == ["failed"]
    assert seen == ["small", "small"]  # scout, then the one mcu attempt


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

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text=""):
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

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text=""):
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

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text=""):
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

    async def one_task(*, endpoint, prompt, pdf_bytes, filename, schema, page_text=""):
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
    assert value is None and "unterminated" in err


def test_reasoning_prose_before_the_json_is_skipped() -> None:
    value, err = datasheets.first_json_value(
        'Let me work through the table.\nThe first pin is GND.\n[{"name": "GND"}]'
    )
    assert not err and value == [{"name": "GND"}]
