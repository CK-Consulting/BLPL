"""FastAPI backend for BLPL.

Launched via ``blpl serve`` (or ``uvicorn blpl.webapp.main:app --reload`` for
development). Binds 127.0.0.1 by default — no external network exposure. All
filesystem access flows through ``references.FilesystemSandbox``, which
enforces the project's ``references.json`` + the user's global allow/deny
lists.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import StreamingResponse, PlainTextResponse, JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ImportError as exc:  # pragma: no cover — fastapi is an extras dep
    raise RuntimeError(
        "blpl.webapp requires the 'webapp' extras. Install with: "
        "uv pip install -e '.[webapp]'"
    ) from exc

from .conversations import Conversation, list_conversations
from .projects import discover_projects, load_project
from .references import (
    FilesystemSandbox,
    Reference,
    ReferenceManifest,
    ReferencePolicyError,
    load_global_allowlist,
    load_global_denylist,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _resolve_workspace_root() -> Path:
    value = os.environ.get("BLPL_WORKSPACE")
    if value:
        return Path(value).expanduser().resolve()
    # Default: the directory containing the blpl package's parent (repo root).
    here = Path(__file__).resolve()
    # blpl/webapp/main.py → blpl → repo_root
    return here.parents[2]


WORKSPACE_ROOT = _resolve_workspace_root()


# ---------------------------------------------------------------------------
# App + helpers
# ---------------------------------------------------------------------------


app = FastAPI(title="Board Layer Pipe Line (BLPL) API", version="0.3.0")


def _require_project(project_id: str):
    proj = load_project(WORKSPACE_ROOT, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"project {project_id!r} not found")
    return proj


def _sandbox_for(proj) -> FilesystemSandbox:
    return FilesystemSandbox(
        manifest=proj.manifest,
        global_allowlist=load_global_allowlist(),
        global_denylist=load_global_denylist(),
    )


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "workspace_root": str(WORKSPACE_ROOT)}


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@app.get("/api/projects")
def list_projects() -> list[dict]:
    return [p.to_dict() for p in discover_projects(WORKSPACE_ROOT)]


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict:
    proj = _require_project(project_id)
    pipeline_artifacts = []
    if proj.pipeline_dir.exists():
        pipeline_artifacts = sorted(p.name for p in proj.pipeline_dir.iterdir() if p.is_file())
    return {
        **proj.to_dict(),
        "artifacts": pipeline_artifacts,
        "has_pipeline_dir": proj.pipeline_dir.exists(),
    }


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


class ReferenceInput(BaseModel):
    name: str
    path: str
    role: str = "other"
    access: str = "read"
    scope: str = "project"
    materialize: bool = False


class ReferenceManifestInput(BaseModel):
    references: list[ReferenceInput]


@app.get("/api/projects/{project_id}/references")
def get_references(project_id: str) -> dict:
    proj = _require_project(project_id)
    return {
        "project_id": proj.project_id,
        "workspace_root": str(proj.manifest.workspace_root),
        "references": [r.to_dict() for r in proj.manifest.references],
    }


@app.put("/api/projects/{project_id}/references")
def put_references(project_id: str, payload: ReferenceManifestInput) -> dict:
    proj = _require_project(project_id)
    try:
        refs = [
            Reference(
                name=r.name,
                path=Path(r.path).expanduser().resolve(),
                role=r.role,
                access=r.access,
                scope=r.scope,
                materialize=r.materialize,
            )
            for r in payload.references
        ]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    proj.manifest.references = refs
    proj.ensure_blpl_dirs()
    proj.manifest.save(proj.references_path)
    return {"references": [r.to_dict() for r in refs], "saved_to": str(proj.references_path)}


# ---------------------------------------------------------------------------
# Artifacts (read-only, sandboxed)
# ---------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/artifacts")
def list_artifacts(project_id: str) -> dict:
    proj = _require_project(project_id)
    if not proj.pipeline_dir.exists():
        return {"artifacts": []}
    artifacts = [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted(proj.pipeline_dir.iterdir())
        if p.is_file()
    ]
    return {"artifacts": artifacts}


@app.get("/api/projects/{project_id}/artifacts/{artifact_name}")
def read_artifact(project_id: str, artifact_name: str):
    proj = _require_project(project_id)
    sandbox = _sandbox_for(proj)
    target = proj.pipeline_dir / artifact_name
    try:
        path = sandbox.check_read(target)
    except ReferencePolicyError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"artifact {artifact_name!r} not found")
    suffix = path.suffix.lower()
    content = path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".json":
        try:
            return JSONResponse(content=json.loads(content))
        except json.JSONDecodeError:
            pass
    return PlainTextResponse(content=content)


# ---------------------------------------------------------------------------
# Stage runner (SSE)
# ---------------------------------------------------------------------------


_VALID_STAGES = {
    "stage0-det", "stage0-llm", "stage0-compare",
    "stage1", "stage1-synthesize-connectors",
    "stage2", "stage3", "stage4", "stage5",
    "stage6", "stage6-plugin", "stage7", "stage8",
    "resolve-pin-map",
}


@app.post("/api/projects/{project_id}/stages/{stage_name}")
def run_stage(project_id: str, stage_name: str, request: Request) -> StreamingResponse:
    if stage_name not in _VALID_STAGES:
        raise HTTPException(status_code=400, detail=f"unknown stage {stage_name!r}")
    proj = _require_project(project_id)

    def _iter_events():
        """Yield SSE-formatted stdout/stderr lines from the stage subprocess."""
        cmd = ["blpl", stage_name, "--project-dir", str(proj.root)]
        yield f"event: start\ndata: {json.dumps({'cmd': cmd})}\n\n"
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                yield f"event: log\ndata: {json.dumps({'line': line.rstrip()})}\n\n"
        finally:
            rc = proc.wait()
            yield f"event: done\ndata: {json.dumps({'exit_code': rc})}\n\n"

    return StreamingResponse(_iter_events(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


class NewConversationInput(BaseModel):
    title: str = "conversation"


class MessageInput(BaseModel):
    role: str
    content: str
    metadata: dict[str, Any] | None = None


@app.get("/api/projects/{project_id}/conversations")
def get_conversations(project_id: str) -> list[dict]:
    proj = _require_project(project_id)
    return [m.to_dict() for m in list_conversations(proj.conversations_dir)]


@app.post("/api/projects/{project_id}/conversations")
def create_conversation(project_id: str, payload: NewConversationInput) -> dict:
    proj = _require_project(project_id)
    proj.ensure_blpl_dirs()
    conv = Conversation.create(proj.conversations_dir, title=payload.title)
    return {
        "slug": conv.slug,
        "filename": conv.path.name,
        "started_at": conv.started_at,
        "path": str(conv.path),
    }


@app.get("/api/projects/{project_id}/conversations/{filename}")
def read_conversation(project_id: str, filename: str) -> dict:
    proj = _require_project(project_id)
    try:
        conv = Conversation.open_existing(proj.conversations_dir, filename)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "slug": conv.slug,
        "filename": conv.path.name,
        "started_at": conv.started_at,
        "events": conv.read_all(),
    }


@app.post("/api/projects/{project_id}/conversations/{filename}/messages")
def append_message(
    project_id: str, filename: str, payload: MessageInput
) -> dict:
    proj = _require_project(project_id)
    try:
        conv = Conversation.open_existing(proj.conversations_dir, filename)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return conv.append(payload.role, payload.content, payload.metadata)


# ---------------------------------------------------------------------------
# Sandbox diagnostics
# ---------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/sandbox")
def sandbox_summary(project_id: str) -> dict:
    proj = _require_project(project_id)
    return _sandbox_for(proj).summary()


# ---------------------------------------------------------------------------
# Serve the built SPA
# ---------------------------------------------------------------------------
#
# Layout: blpl/webapp/main.py and blpl/webapp/static/ (vite build output).
# Mounted last so the /api/* routes take precedence.

_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(_STATIC_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        """Serve index.html for any non-API path — client-side routing handles the rest.

        Refuses to match anything under /api/ so unknown API routes 404 cleanly
        rather than getting swallowed by the SPA.
        """
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail=f"no API route /{full_path}")
        index = _STATIC_DIR / "index.html"
        if not index.exists():
            raise HTTPException(status_code=404, detail="UI not built. Run `npm run build` in ui/.")
        # Serve a file directly if it exists in the static dir; else SPA fallback.
        target = _STATIC_DIR / full_path
        if full_path and target.exists() and target.is_file():
            return FileResponse(target)
        return FileResponse(index)
