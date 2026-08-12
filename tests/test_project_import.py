"""The browser-upload import path: files (or a zip of the project folder) become
a git-backed working copy whose first commit is the imported state.

The path/suffix policy itself lives in app/backend/app/importer.py as pure
functions; these tests exercise it through the real endpoint so what is asserted
is what a browser actually gets.
"""

from __future__ import annotations

import io
import zipfile


def _unlock(client) -> None:
    client.post("/api/auth/initialize", json={"passphrase": "the-real-one"})


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# -- the happy paths ----------------------------------------------------------


def test_import_creates_a_committed_git_project(client) -> None:
    _unlock(client)
    r = client.post(
        "/api/projects/import",
        data={"name": "dev04"},
        files=[
            ("files", ("design.md", b"# board\n", "text/markdown")),
            ("files", ("project.yaml", b"project:\n", "application/yaml")),
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["imported"] == 2 and body["skipped"] == []

    listing = client.get("/api/projects").json()
    proj = next(p for p in listing if p["id"] == "dev04")
    assert proj["is_git"] and proj["markdown_files"] == 1

    # The import is the first commit: a clean tree, nothing uncommitted.
    st = client.get("/api/projects/dev04/git/status").json()
    assert st["dirty"] is False and st["has_remote"] is False

    # And the files are readable through the normal editor endpoints.
    assert client.get("/api/projects/dev04/files/design.md").json()["content"] == "# board\n"


def test_a_common_wrapping_folder_is_stripped(client) -> None:
    """Selecting a folder (or zipping one) wraps everything in one directory —
    but the pipeline reads *.md from the project root, so the wrapper must go."""
    _unlock(client)
    r = client.post(
        "/api/projects/import",
        data={"name": "dev04"},
        files=[
            ("files", ("myboard/design.md", b"# board\n", "text/markdown")),
            ("files", ("myboard/notes/pinouts.md", b"# pinouts\n", "text/markdown")),
        ],
    )
    assert r.status_code == 200
    assert sorted(r.json()["files"]) == ["design.md", "notes/pinouts.md"]
    proj = next(p for p in client.get("/api/projects").json() if p["id"] == "dev04")
    assert proj["markdown_files"] == 1  # design.md is at the root where Stage 0 looks


def test_a_zip_of_the_project_folder_imports(client) -> None:
    _unlock(client)
    blob = _zip_bytes(
        {
            "myboard/design.md": b"# board\n",
            "myboard/refs/lc76g.pdf": b"%PDF-1.4 fake",
        }
    )
    r = client.post(
        "/api/projects/import",
        data={"name": "dev04"},
        files=[("files", ("myboard.zip", blob, "application/zip"))],
    )
    assert r.status_code == 200
    assert sorted(r.json()["files"]) == ["design.md", "refs/lc76g.pdf"]


# -- what gets refused, and how loudly ----------------------------------------


def test_unsafe_and_non_design_files_are_skipped_with_reasons(client) -> None:
    """Nothing is dropped silently — the Stage 0 lesson applies to uploads too."""
    _unlock(client)
    r = client.post(
        "/api/projects/import",
        data={"name": "dev04"},
        files=[
            ("files", ("design.md", b"# board\n", "text/markdown")),
            ("files", ("../escape.md", b"x", "text/markdown")),
            ("files", (".git/config", b"x", "text/plain")),
            ("files", ("tool.py", b"print()", "text/x-python")),
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["files"] == ["design.md"]
    skipped = {s["name"]: s["reason"] for s in body["skipped"]}
    assert set(skipped) == {"../escape.md", ".git/config", "tool.py"}
    assert "unsafe path" in skipped["../escape.md"]
    assert "unsafe path" in skipped[".git/config"]
    assert "not a design file" in skipped["tool.py"]


def test_an_upload_with_nothing_importable_is_a_400_and_leaves_no_project(client) -> None:
    _unlock(client)
    r = client.post(
        "/api/projects/import",
        data={"name": "dev04"},
        files=[("files", ("tool.py", b"print()", "text/x-python"))],
    )
    assert r.status_code == 400
    assert "tool.py" in r.json()["detail"]
    assert not any(p["id"] == "dev04" for p in client.get("/api/projects").json())


def test_import_refuses_to_clobber_an_existing_project(client) -> None:
    _unlock(client)
    client.post("/api/projects/init", json={"name": "dev04"})
    r = client.post(
        "/api/projects/import",
        data={"name": "dev04"},
        files=[("files", ("design.md", b"# board\n", "text/markdown"))],
    )
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


def test_import_rejects_a_nested_project_name(client) -> None:
    _unlock(client)
    r = client.post(
        "/api/projects/import",
        data={"name": "a/b"},
        files=[("files", ("design.md", b"# board\n", "text/markdown"))],
    )
    assert r.status_code == 400


def test_oversize_upload_is_rejected(client, monkeypatch) -> None:
    import app.importer as importer

    _unlock(client)
    monkeypatch.setattr(importer, "MAX_TOTAL_BYTES", 10)
    r = client.post(
        "/api/projects/import",
        data={"name": "dev04"},
        files=[("files", ("design.md", b"#" * 100, "text/markdown"))],
    )
    assert r.status_code == 400
    assert "MB" in r.json()["detail"]


def test_a_zip_declaring_more_than_the_cap_is_rejected_before_extraction(client, monkeypatch) -> None:
    import app.importer as importer

    _unlock(client)
    monkeypatch.setattr(importer, "MAX_TOTAL_BYTES", 1024)
    blob = _zip_bytes({"design.md": b"#" * 4096})  # compresses tiny, declares 4 KiB
    r = client.post(
        "/api/projects/import",
        data={"name": "dev04"},
        files=[("files", ("board.zip", blob, "application/zip"))],
    )
    assert r.status_code == 400
    assert "decompresses" in r.json()["detail"]


def test_import_requires_an_unlocked_session(client) -> None:
    r = client.post(
        "/api/projects/import",
        data={"name": "dev04"},
        files=[("files", ("design.md", b"# board\n", "text/markdown"))],
    )
    assert r.status_code == 401
