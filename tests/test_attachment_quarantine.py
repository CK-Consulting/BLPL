"""Chat attachments pass the same checks as uploaded files.

The attachment store took anything that sniffed as a PDF or image, so a file the
upload quarantine held — in one project, an encrypted PDF it could not inspect —
could be dropped into the chat instead and read by the assistant.
"""

from __future__ import annotations

import pytest

from app import attachments

CLEAN = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
ACTS_ON_OPEN = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/OpenAction"
    b"<</S/JavaScript/JS(app.alert(1))>>>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
ENCRYPTED = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"trailer<</Root 1 0 R/Encrypt 5 0 R>>\n%%EOF\n"
)


@pytest.fixture(autouse=True)
def no_scanner(monkeypatch):
    monkeypatch.delenv("BLPL_CLAMD_SOCKET", raising=False)
    monkeypatch.delenv("BLPL_CLAMD_HOST", raising=False)
    monkeypatch.delenv("BLPL_QUARANTINE_REQUIRE_SCAN", raising=False)


def test_a_clean_pdf_is_attached(tmp_path):
    a = attachments.save(tmp_path, "datasheet.pdf", CLEAN)
    assert attachments.path_of(tmp_path, a.id) is not None


@pytest.mark.parametrize(
    "data, why", [(ACTS_ON_OPEN, "opened"), (ENCRYPTED, "encrypted")], ids=["acts-on-open", "encrypted"]
)
def test_a_pdf_the_quarantine_would_hold_is_not_attached(tmp_path, data, why):
    with pytest.raises(attachments.AttachmentRejected) as exc:
        attachments.save(tmp_path, "vendor.pdf", data)
    assert why in str(exc.value)
    assert not list(attachments.store_dir(tmp_path).glob("*.pdf"))


def test_assessment_runs_off_the_event_loop(unlocked, monkeypatch):
    # Codex P1 on #8: saving now inflates PDF streams and waits on the scanner,
    # and the async route called it inline — ten slow files blocked every
    # other request. On a worker thread there is no running event loop.
    import asyncio

    import app.main as main

    seen = []
    real = main.attachments.save

    def spy(*a, **kw):
        try:
            asyncio.get_running_loop()
            seen.append("event loop")
        except RuntimeError:
            seen.append("worker thread")
        return real(*a, **kw)

    monkeypatch.setattr(main.attachments, "save", spy)
    unlocked.post("/api/projects/init", json={"name": "scratch"})
    conv = unlocked.post("/api/projects/scratch/conversations", json={"title": "t"}).json()["filename"]
    r = unlocked.post(
        f"/api/projects/scratch/conversations/{conv}/attachments",
        files=[("files", ("a.pdf", CLEAN, "application/pdf"))],
    )
    assert r.status_code == 200, r.text
    assert seen == ["worker thread"]
