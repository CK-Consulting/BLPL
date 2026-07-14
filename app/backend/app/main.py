"""BLPL app backend — generate, review, and see a board in one place.

This is the vertical slice: markdown in, KiCad out, rendered in the browser.

Design notes:

* KiCad lives in the *image*, not on your laptop. The container is built FROM
  kicad/kicad:10.0.0, so kicad-cli is always present and always the same version
  no matter which workstation you are sitting at. That is the whole point.

* The viewer is client-side. ecad-viewer parses .kicad_sch/.kicad_pcb in the
  browser, so these endpoints serve raw file *text* — no server-side rasterising
  on the hot path. Server-side kicad-cli is reserved for the things only KiCad
  can do (ERC/DRC, SVG diffs, GLB export).

* Stage execution streams over SSE. KiCAD-Prism polls a job dict for this and
  writes a SQLite row per log line; BLPL already had a streaming runner, so we
  keep ours.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse

app = FastAPI(title="BLPL", version="0.4.0")

# Dev convenience: the Vite dev server runs on another origin. In the container
# both are behind one nginx, so this is a no-op in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PROJECTS_ROOT = Path(os.environ.get("BLPL_PROJECTS_ROOT", "/app/projects")).resolve()

# Every stage the UI is allowed to invoke. An allowlist, not a passthrough —
# stage_name lands in an argv, so it must never be user-controlled free text.
VALID_STAGES = {
    "doctor",
    "stage0-det", "stage0-llm", "stage0-compare",
    "stage1", "stage1-synthesize-connectors",
    "stage2", "stage3", "stage4", "stage5",
    "stage6", "stage6-plugin", "stage7", "stage8",
}


def _project_dir(project_id: str) -> Path:
    """Resolve a project id to a directory, refusing anything outside the root.

    project_id arrives from the URL, so `../../etc` is on the table. Resolve
    first, then prove the result is still under PROJECTS_ROOT.
    """
    candidate = (PROJECTS_ROOT / project_id).resolve()
    if not candidate.is_relative_to(PROJECTS_ROOT):
        raise HTTPException(status_code=400, detail="invalid project id")
    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    return candidate


def _latest(pipeline_dir: Path, suffix: str) -> Path | None:
    """Newest timestamped emission of a given kind, or None."""
    if not pipeline_dir.is_dir():
        return None
    files = sorted(pipeline_dir.glob(f"*{suffix}"), reverse=True)
    return files[0] if files else None


@app.get("/api/health")
def health() -> dict:
    kicad = subprocess.run(
        ["kicad-cli", "--version"], capture_output=True, text=True, check=False
    )
    return {
        "status": "ok",
        "projects_root": str(PROJECTS_ROOT),
        # Surfaced deliberately: "which KiCad am I actually talking to" is the
        # question you ask when a board renders differently than you expect.
        "kicad_cli": kicad.stdout.strip() or None,
    }


@app.get("/api/projects")
def list_projects() -> list[dict]:
    if not PROJECTS_ROOT.is_dir():
        return []
    out = []
    for d in sorted(PROJECTS_ROOT.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        pipeline = d / ".pipeline"
        out.append(
            {
                "id": d.name,
                "markdown_files": len(list(d.glob("*.md"))),
                "has_schematic": _latest(pipeline, ".kicad_sch") is not None,
                "has_pcb": _latest(pipeline, ".kicad_pcb") is not None,
            }
        )
    return out


@app.get("/api/projects/{project_id}/artifacts/{name}")
def read_artifact(project_id: str, name: str):
    """Read one .pipeline artifact (review_report.json, gaps.md, ...)."""
    proj = _project_dir(project_id)
    target = (proj / ".pipeline" / name).resolve()
    if not target.is_relative_to(proj / ".pipeline") or not target.is_file():
        raise HTTPException(status_code=404, detail=f"no artifact {name!r}")
    text = target.read_text(encoding="utf-8", errors="replace")
    if target.suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return PlainTextResponse(text)


@app.get("/api/projects/{project_id}/design")
def design_sources(project_id: str) -> dict:
    """Raw KiCad file text for ecad-viewer to parse client-side.

    Returned as {filename, content} pairs because that is exactly the shape the
    <ecad-blob> element wants — the frontend does no reshaping.
    """
    proj = _project_dir(project_id)
    pipeline = proj / ".pipeline"

    sources = []
    for suffix in (".kicad_sch", ".kicad_pcb"):
        f = _latest(pipeline, suffix)
        if f is not None:
            sources.append(
                {"filename": f.name, "content": f.read_text(encoding="utf-8", errors="replace")}
            )
    if not sources:
        raise HTTPException(
            status_code=404,
            detail="no emitted KiCad files — run stage6 first",
        )
    return {"sources": sources}


@app.post("/api/projects/{project_id}/stages/{stage_name}")
async def run_stage(project_id: str, stage_name: str) -> StreamingResponse:
    """Run a pipeline stage, streaming its output to the browser as it happens."""
    if stage_name not in VALID_STAGES:
        raise HTTPException(status_code=400, detail=f"unknown stage {stage_name!r}")
    proj = _project_dir(project_id)

    async def events():
        cmd = [sys.executable, "-m", "blpl.core.cli", stage_name, "--project-dir", str(proj)]
        yield f"event: start\ndata: {json.dumps({'cmd': cmd})}\n\n"

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                yield f"event: log\ndata: {json.dumps({'line': line})}\n\n"
            code = await proc.wait()
            yield f"event: done\ndata: {json.dumps({'exit_code': code})}\n\n"
        finally:
            # If the client disconnects mid-run, don't leave kicad-cli or an LLM
            # call orphaned. Prism leaks these; we shouldn't.
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    return StreamingResponse(events(), media_type="text/event-stream")
