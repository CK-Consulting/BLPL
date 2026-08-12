"""FilesystemSandbox unit tests, plus the reference/conversation/artifact API.

These used to test a second FastAPI backend under ``blpl/webapp`` that served the
same React bundle against an API with no auth, no git, and no ``/design`` — so
the UI's first request 404'd and the app dead-ended. That backend is gone; its
modules and routes now live in ``app/backend`` behind the session gate, and these
tests follow them there.

The sandbox tests below are unchanged in substance: ``references.py`` moved
without edits, so its contract is still exactly what it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")  # webapp extras must be installed for these tests.

from app.references import (  # noqa: E402  (import after importorskip)
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
# API tests — the ported routes, now behind the session gate
# ---------------------------------------------------------------------------


def _seed_project(client, name: str = "alpha") -> Path:
    """Create a project directory under the app's projects root."""
    import app.main as main

    proj = main.PROJECTS_ROOT / name
    (proj / ".pipeline").mkdir(parents=True, exist_ok=True)
    (proj / ".pipeline" / "bom.json").write_text(
        json.dumps({"project_id": name, "schema_version": 1, "rows": []})
    )
    return proj


def test_references_require_an_unlocked_session(client) -> None:
    """The gate is the point: these routes had no auth at all before the merge."""
    assert client.get("/api/projects/alpha/references").status_code == 401
    assert client.get("/api/projects/alpha/conversations").status_code == 401
    assert client.get("/api/projects/alpha/sandbox").status_code == 401


def test_references_default_to_empty_for_a_new_project(unlocked) -> None:
    _seed_project(unlocked)
    r = unlocked.get("/api/projects/alpha/references")
    assert r.status_code == 200
    assert r.json()["references"] == []


def test_put_references_saves_manifest(unlocked, tmp_path: Path) -> None:
    proj = _seed_project(unlocked)
    external = tmp_path / "external_dir"
    external.mkdir()
    payload = {
        "references": [
            {"name": "ref1", "path": str(external), "role": "reference_design", "access": "read"}
        ]
    }
    r = unlocked.put("/api/projects/alpha/references", json=payload)
    assert r.status_code == 200
    assert (proj / ".blpl" / "references.json").is_file()

    again = unlocked.get("/api/projects/alpha/references").json()
    assert [x["name"] for x in again["references"]] == ["ref1"]


def test_references_reject_invalid_access(unlocked, tmp_path: Path) -> None:
    _seed_project(unlocked)
    external = tmp_path / "ext"
    external.mkdir()
    payload = {"references": [{"name": "r", "path": str(external), "access": "chmod777"}]}
    assert unlocked.put("/api/projects/alpha/references", json=payload).status_code == 400


def test_a_corrupt_manifest_degrades_to_empty(unlocked) -> None:
    """An unreadable references.json costs you references, not access to the board."""
    proj = _seed_project(unlocked)
    (proj / ".blpl").mkdir(parents=True, exist_ok=True)
    (proj / ".blpl" / "references.json").write_text("{not json")
    r = unlocked.get("/api/projects/alpha/references")
    assert r.status_code == 200
    assert r.json()["references"] == []


def test_list_artifacts_reports_name_size_and_created(unlocked) -> None:
    _seed_project(unlocked)
    r = unlocked.get("/api/projects/alpha/artifacts")
    assert r.status_code == 200
    arts = r.json()["artifacts"]
    assert [a["name"] for a in arts] == ["bom.json"]
    assert arts[0]["size"] > 0
    assert arts[0]["created"].endswith("Z")


def test_list_artifacts_excludes_rotated_copies(unlocked) -> None:
    """archive/ holds previous runs; listing them drowns the current output."""
    proj = _seed_project(unlocked)
    archive = proj / ".pipeline" / "archive"
    archive.mkdir()
    (archive / "old.kicad_sch").write_text("(kicad_sch)")
    names = [a["name"] for a in unlocked.get("/api/projects/alpha/artifacts").json()["artifacts"]]
    assert names == ["bom.json"]


def test_design_falls_back_to_the_archive_and_says_so(unlocked) -> None:
    """The dev.04 case: nine emitted boards, all rotated, app claimed there were none."""
    proj = _seed_project(unlocked)
    archive = proj / ".pipeline" / "archive"
    archive.mkdir()
    (archive / "board_2026-07-14_141235Z.kicad_sch").write_text("(kicad_sch)")
    (archive / "board_2026-07-14_141235Z.kicad_pcb").write_text("(kicad_pcb)")

    r = unlocked.get("/api/projects/alpha/design")
    assert r.status_code == 200
    body = r.json()
    assert body["archived"] is True
    assert {s["filename"] for s in body["sources"]} == {
        "board_2026-07-14_141235Z.kicad_sch",
        "board_2026-07-14_141235Z.kicad_pcb",
    }


def test_design_prefers_the_live_output_over_the_archive(unlocked) -> None:
    proj = _seed_project(unlocked)
    pipeline = proj / ".pipeline"
    (pipeline / "archive").mkdir()
    (pipeline / "archive" / "old.kicad_sch").write_text("(old)")
    (pipeline / "new.kicad_sch").write_text("(new)")

    body = unlocked.get("/api/projects/alpha/design").json()
    assert body["archived"] is False
    assert [s["filename"] for s in body["sources"]] == ["new.kicad_sch"]


def test_read_artifact_returns_parsed_json(unlocked) -> None:
    _seed_project(unlocked)
    r = unlocked.get("/api/projects/alpha/artifacts/bom.json")
    assert r.status_code == 200
    assert r.json()["project_id"] == "alpha"


def test_read_artifact_refuses_path_escape(unlocked, tmp_path: Path) -> None:
    _seed_project(unlocked)
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    r = unlocked.get("/api/projects/alpha/artifacts/../../../secret.txt")
    assert r.status_code == 404


def test_conversation_create_append_and_read(unlocked) -> None:
    _seed_project(unlocked)
    created = unlocked.post(
        "/api/projects/alpha/conversations", json={"title": "barrel jack pinout"}
    )
    assert created.status_code == 200
    filename = created.json()["filename"]
    assert created.json()["slug"] == "barrel-jack-pinout"

    appended = unlocked.post(
        f"/api/projects/alpha/conversations/{filename}/messages",
        json={"role": "user", "content": "what pin is VBUS?"},
    )
    assert appended.status_code == 200

    events = unlocked.get(f"/api/projects/alpha/conversations/{filename}").json()["events"]
    assert [e["content"] for e in events] == ["what pin is VBUS?"]


def test_list_conversations(unlocked) -> None:
    _seed_project(unlocked)
    unlocked.post("/api/projects/alpha/conversations", json={"title": "one"})
    metas = unlocked.get("/api/projects/alpha/conversations").json()
    assert [m["slug"] for m in metas] == ["one"]


def test_conversation_filename_must_match_the_expected_shape(unlocked) -> None:
    _seed_project(unlocked)
    r = unlocked.get("/api/projects/alpha/conversations/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code == 404


def test_sandbox_endpoint_returns_policy_summary(unlocked) -> None:
    _seed_project(unlocked)
    r = unlocked.get("/api/projects/alpha/sandbox")
    assert r.status_code == 200
    assert "workspace_root" in r.json()


@pytest.mark.parametrize("method", ["get", "post", "put", "delete", "patch"])
def test_unknown_api_routes_are_404_for_every_method(unlocked, method: str) -> None:
    """Not 405, and not the SPA shell.

    `blpl serve` mounts the built frontend on the same port with a GET-only
    catch-all. Left unguarded, that catch-all matches an unknown /api path but
    rejects the verb, so every unknown POST/PUT/DELETE came back 405 — but only
    on machines where app/frontend/dist happened to exist. Whether a frontend is
    built must not change API semantics.
    """
    r = getattr(unlocked, method)("/api/definitely/not/a/route")
    assert r.status_code == 404


def test_unknown_non_api_routes_do_not_leak_api_errors(unlocked) -> None:
    """A client-side route must not 404 — the SPA owns it (when one is built)."""
    r = unlocked.get("/projects/alpha/board")
    assert r.status_code in (200, 404)  # 200 with a build present, 404 without
    assert "no API route" not in r.text
