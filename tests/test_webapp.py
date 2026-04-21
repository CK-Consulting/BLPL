"""Tests for blpl.webapp — FastAPI backend + FilesystemSandbox."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")  # webapp extras must be installed for these tests.

from fastapi.testclient import TestClient

from blpl.webapp.references import (
    FilesystemSandbox,
    Reference,
    ReferenceManifest,
    ReferencePolicyError,
)


# ---------------------------------------------------------------------------
# FilesystemSandbox — unit tests (no FastAPI)
# ---------------------------------------------------------------------------


def _make_manifest(workspace_root: Path, refs: list[Reference]) -> ReferenceManifest:
    return ReferenceManifest(
        project_id="test",
        workspace_root=workspace_root.resolve(),
        references=refs,
    )


def test_sandbox_allows_reads_inside_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    (ws / "file.md").write_text("hi")
    sb = FilesystemSandbox(_make_manifest(ws, []))
    resolved = sb.check_read(ws / "file.md")
    assert resolved == (ws / "file.md").resolve()


def test_sandbox_rejects_reads_outside_manifest(tmp_path: Path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    sb = FilesystemSandbox(_make_manifest(ws, []))
    with pytest.raises(ReferencePolicyError):
        sb.check_read(outside / "file.txt")


def test_sandbox_allows_read_inside_declared_reference(tmp_path: Path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    ref_root = tmp_path / "reference"
    ref_root.mkdir()
    (ref_root / "file").write_text("hi")
    ref = Reference(name="ref", path=ref_root.resolve(), access="read")
    sb = FilesystemSandbox(_make_manifest(ws, [ref]))
    assert sb.check_read(ref_root / "file") == (ref_root / "file").resolve()


def test_sandbox_refuses_write_to_readonly_reference(tmp_path: Path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    ref_root = tmp_path / "readonly_ref"
    ref_root.mkdir()
    ref = Reference(name="r", path=ref_root.resolve(), access="read")
    sb = FilesystemSandbox(_make_manifest(ws, [ref]))
    with pytest.raises(ReferencePolicyError):
        sb.check_write(ref_root / "newfile")


def test_sandbox_allows_write_to_readwrite_reference(tmp_path: Path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    ref_root = tmp_path / "rw_ref"
    ref_root.mkdir()
    ref = Reference(name="r", path=ref_root.resolve(), access="read-write")
    sb = FilesystemSandbox(_make_manifest(ws, [ref]))
    assert sb.check_write(ref_root / "newfile") == (ref_root / "newfile").resolve()


def test_sandbox_allows_writes_inside_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    sb = FilesystemSandbox(_make_manifest(ws, []))
    assert sb.check_write(ws / "generated.json") == (ws / "generated.json").resolve()


def test_sandbox_delete_refuses_pre_session_files(tmp_path: Path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    existing = ws / "pre.txt"
    existing.write_text("before session")
    sb = FilesystemSandbox(_make_manifest(ws, []))
    # Even though workspace is writable, pre-existing files cannot be deleted.
    with pytest.raises(ReferencePolicyError):
        sb.check_delete(existing)


def test_sandbox_delete_allows_session_created_files(tmp_path: Path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    sb = FilesystemSandbox(_make_manifest(ws, []))
    created = ws / "just-made.json"
    sb.register_creation(created)
    assert sb.check_delete(created) == created.resolve()


def test_sandbox_global_denylist_wins(tmp_path: Path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    sensitive = ws / "nested"
    sensitive.mkdir()
    sb = FilesystemSandbox(
        _make_manifest(ws, []), global_denylist=[sensitive.resolve()]
    )
    with pytest.raises(ReferencePolicyError):
        sb.check_read(sensitive / "file")


# ---------------------------------------------------------------------------
# ReferenceManifest — round-trip
# ---------------------------------------------------------------------------


def test_manifest_roundtrip_json(tmp_path: Path) -> None:
    ws = tmp_path / "project"
    ws.mkdir()
    ref = Reference(name="r1", path=(tmp_path / "ref").resolve(), role="symbol_source", access="read-write")
    m = _make_manifest(ws, [ref])
    path = tmp_path / "manifest.json"
    m.save(path)
    loaded = ReferenceManifest.load(path)
    assert loaded.project_id == m.project_id
    assert len(loaded.references) == 1
    assert loaded.references[0].name == "r1"
    assert loaded.references[0].access == "read-write"


# ---------------------------------------------------------------------------
# FastAPI endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture
def webapp_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with two minimal projects for the FastAPI tests."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    for name in ("alpha", "beta"):
        proj = ws / name
        (proj / ".blpl").mkdir(parents=True)
        (proj / ".pipeline").mkdir(parents=True)
        (proj / ".pipeline" / "bom.json").write_text(json.dumps({"project_id": name, "schema_version": 1, "rows": []}))
    monkeypatch.setenv("BLPL_WORKSPACE", str(ws))
    # Force re-import so main.py picks up the env var fresh.
    import importlib
    import blpl.webapp.main as _main
    importlib.reload(_main)
    return ws


def test_list_projects_returns_both(webapp_workspace: Path) -> None:
    from blpl.webapp.main import app
    client = TestClient(app)
    r = client.get("/api/projects")
    assert r.status_code == 200
    names = {p["project_id"] for p in r.json()}
    assert names == {"alpha", "beta"}


def test_get_project_includes_artifacts(webapp_workspace: Path) -> None:
    from blpl.webapp.main import app
    client = TestClient(app)
    r = client.get("/api/projects/alpha")
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == "alpha"
    assert "bom.json" in body["artifacts"]


def test_get_project_not_found(webapp_workspace: Path) -> None:
    from blpl.webapp.main import app
    client = TestClient(app)
    r = client.get("/api/projects/nonexistent")
    assert r.status_code == 404


def test_put_references_saves_manifest(webapp_workspace: Path) -> None:
    from blpl.webapp.main import app
    client = TestClient(app)
    external = webapp_workspace.parent / "external_dir"
    external.mkdir()
    payload = {
        "references": [
            {"name": "ext", "path": str(external), "role": "reference_design", "access": "read", "scope": "project"}
        ]
    }
    r = client.put("/api/projects/alpha/references", json=payload)
    assert r.status_code == 200, r.text
    # Confirm it was written to disk.
    saved = webapp_workspace / "alpha" / ".blpl" / "references.json"
    assert saved.exists()
    data = json.loads(saved.read_text())
    assert data["references"][0]["name"] == "ext"


def test_references_rejects_invalid_access(webapp_workspace: Path) -> None:
    from blpl.webapp.main import app
    client = TestClient(app)
    r = client.put(
        "/api/projects/alpha/references",
        json={"references": [{"name": "x", "path": "/tmp", "access": "nuclear"}]},
    )
    assert r.status_code == 400


def test_read_artifact_returns_json(webapp_workspace: Path) -> None:
    from blpl.webapp.main import app
    client = TestClient(app)
    r = client.get("/api/projects/alpha/artifacts/bom.json")
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == "alpha"


def test_read_artifact_refuses_path_escape(webapp_workspace: Path) -> None:
    from blpl.webapp.main import app
    client = TestClient(app)
    # Path traversal attempt — FastAPI routing typically sanitizes, but test the
    # sandbox layer explicitly via a nonexistent artifact name.
    r = client.get("/api/projects/alpha/artifacts/../../../etc/passwd")
    # Either 404 (route didn't match a file) or 403 (sandbox refused). Both are safe.
    assert r.status_code in (404, 403)


def test_conversation_create_and_append(webapp_workspace: Path) -> None:
    from blpl.webapp.main import app
    client = TestClient(app)
    # Create
    r = client.post("/api/projects/alpha/conversations", json={"title": "test chat"})
    assert r.status_code == 200
    created = r.json()
    filename = created["filename"]
    # Append
    r2 = client.post(
        f"/api/projects/alpha/conversations/{filename}/messages",
        json={"role": "user", "content": "hello"},
    )
    assert r2.status_code == 200
    # Read back
    r3 = client.get(f"/api/projects/alpha/conversations/{filename}")
    assert r3.status_code == 200
    events = r3.json()["events"]
    assert len(events) == 1 and events[0]["role"] == "user" and events[0]["content"] == "hello"


def test_list_conversations(webapp_workspace: Path) -> None:
    from blpl.webapp.main import app
    client = TestClient(app)
    client.post("/api/projects/alpha/conversations", json={"title": "one"})
    client.post("/api/projects/alpha/conversations", json={"title": "two"})
    r = client.get("/api/projects/alpha/conversations")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_invalid_stage_rejected(webapp_workspace: Path) -> None:
    from blpl.webapp.main import app
    client = TestClient(app)
    r = client.post("/api/projects/alpha/stages/stage999")
    assert r.status_code == 400


def test_sandbox_endpoint_returns_policy_summary(webapp_workspace: Path) -> None:
    from blpl.webapp.main import app
    client = TestClient(app)
    r = client.get("/api/projects/alpha/sandbox")
    assert r.status_code == 200
    body = r.json()
    assert "workspace_root" in body
    assert "manifest_references" in body
