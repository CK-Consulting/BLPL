"""BLPL app backend — generate, review, and see a board from any workstation.

Three things make this the hosted app rather than the earlier vertical slice:

* KiCad lives in the *image*, not on your laptop (FROM kicad/kicad:10.0.0), so
  every workstation you sit at renders and exports identically.

* Your secrets live in an encrypted vault, unlocked by a passphrase you type and
  the server never stores. The whole API sits behind that unlock — the passphrase
  is the app's front door. See app/vault.py and app/identity.py.

* Your projects are git-backed working copies on the server, so they follow you
  between machines. See app/projects.py.

The security posture in one line: everything under /api requires an unlocked
session except the auth handshake and the health probe. A server restart drops
all sessions (the decrypted key is only ever in RAM), and unlocking again is the
way back in.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from . import appconfig, importer, llm_resolver, vault
from .appconfig import AppConfig, ProjectEntry
from .conversations import Conversation, list_conversations
from .identity import Identity, Locked
from .projects import ProjectError, Projects
from .references import (
    FilesystemSandbox,
    Reference,
    ReferenceManifest,
    load_global_allowlist,
    load_global_denylist,
)
from .store import Store

app = FastAPI(title="BLPL", version="0.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,  # the session cookie must ride cross-origin in dev
    allow_methods=["*"],
    allow_headers=["*"],
)

# State roots. All three are volumes on the deploy; all three default under one
# data dir so a bare `docker run` still works.
_DATA = Path(os.environ.get("BLPL_DATA_ROOT", "/app/data"))
PROJECTS_ROOT = Path(os.environ.get("BLPL_PROJECTS_ROOT", str(_DATA / "projects"))).resolve()
VAULT_DB = Path(os.environ.get("BLPL_VAULT_DB", str(_DATA / "vault.db")))
CONFIG_PATH = Path(os.environ.get("BLPL_CONFIG", str(_DATA / "blpl.toml")))

_SESSION_COOKIE = "blpl_session"


def _env_flag(name: str) -> bool:
    """Parse a boolean environment variable the way a human means it.

    ``bool(os.environ.get(name))`` is a trap: every non-empty string is truthy, so
    ``FOO=0`` and ``FOO=false`` both come out True. That exact trap flagged the
    session cookie Secure when someone set BLPL_COOKIE_SECURE=0 to turn it *off* —
    and a Secure cookie is silently dropped over plain HTTP, so the session never
    stuck and every unlock bounced straight back to the lock screen. Only an
    explicit truthy token counts.
    """
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


identity = Identity(store=Store(VAULT_DB))
projects = Projects(PROJECTS_ROOT)

# Environment variable each provider's SDK reads its key from. ollama is keyless.
_PROVIDER_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

VALID_STAGES = {
    "doctor",
    "stage0-det", "stage0-llm", "stage0-compare",
    "stage1", "stage1-synthesize-connectors",
    "stage2", "stage3", "stage4", "stage5",
    "stage6", "stage6-plugin", "stage7", "stage8",
}

# Stages that call an LLM. Only these need a provider key injected; the rest are
# deterministic and run without one.
_LLM_STAGES = {"stage0-llm", "stage0-compare", "stage1", "stage1-synthesize-connectors"}


# --------------------------------------------------------------------------
# Session gate
# --------------------------------------------------------------------------


def require_session(blpl_session: str | None = Cookie(default=None)) -> str:
    """FastAPI dependency: the request must carry a live unlocked session.

    Returns the token so handlers can pull the DEK-backed secret through it. A
    missing or expired session is a 401 — the client then shows the unlock
    screen.
    """
    if not identity.is_unlocked(blpl_session):
        raise HTTPException(status_code=401, detail="locked")
    return blpl_session  # type: ignore[return-value]


def _load_config() -> AppConfig:
    return appconfig.load(CONFIG_PATH)


# --------------------------------------------------------------------------
# Health + auth (the only routes reachable while locked)
# --------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    kicad = subprocess.run(
        ["kicad-cli", "--version"], capture_output=True, text=True, check=False
    )
    return {
        "status": "ok",
        "projects_root": str(PROJECTS_ROOT),
        "kicad_cli": kicad.stdout.strip() or None,
    }


@app.get("/api/auth/status")
def auth_status(blpl_session: str | None = Cookie(default=None)) -> dict:
    """What the client needs to decide which screen to show: is a passphrase set
    yet (first-run vs returning), and is this browser currently unlocked."""
    return {
        "initialized": identity.is_initialized(),
        "unlocked": identity.is_unlocked(blpl_session),
    }


class PassphraseBody(BaseModel):
    passphrase: str


class ChangePassphraseBody(BaseModel):
    old_passphrase: str
    new_passphrase: str


def _cookie_secure(request: Request) -> bool:
    """Whether the session cookie should carry the Secure flag.

    Derived purely from the connection scheme — there is deliberately no manual
    toggle. A Secure cookie is silently dropped by the browser over plain HTTP, so
    flagging it Secure on an HTTP deploy locks the user out, and no setting can be
    right that does that. Scheme detection is self-correcting:

      - plain http  → not Secure  → the cookie is kept, the session works
      - https       → Secure      → the cookie is kept AND protected in transit

    Behind a TLS-terminating proxy the backend sees http, so the proxy must send
    ``X-Forwarded-Proto: https`` for the flag to switch on — our nginx does, and
    that is the one knob. A proxy that fails to forward the scheme gets a working
    (if unmarked) cookie, which is the safe direction to fail; the fix is to
    forward the scheme, not to force the flag on and risk the lockout.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    proto = (forwarded.split(",")[0].strip() or request.scope.get("scheme", "http")).lower()
    return proto == "https"


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    # httpOnly so page JS can never read the token; SameSite=Lax so it rides
    # normal navigation but not cross-site POSTs. Secure is decided by the
    # connection scheme (see _cookie_secure), not a hand-set flag.
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        max_age=8 * 3600,
    )


@app.post("/api/auth/initialize")
def auth_initialize(body: PassphraseBody, request: Request, response: Response) -> dict:
    """First-run: set the passphrase. Refused if one already exists."""
    try:
        token = identity.initialize(body.passphrase)
    except Exception as exc:  # AlreadyInitialized / ValueError
        raise HTTPException(status_code=400, detail=str(exc))
    _set_session_cookie(response, request, token)
    return {"unlocked": True}


@app.post("/api/auth/unlock")
def auth_unlock(body: PassphraseBody, request: Request, response: Response) -> dict:
    try:
        token = identity.unlock(body.passphrase)
    except vault.WrongPassphrase:
        raise HTTPException(status_code=401, detail="wrong passphrase")
    except Exception as exc:  # NotInitialized
        raise HTTPException(status_code=400, detail=str(exc))
    _set_session_cookie(response, request, token)
    return {"unlocked": True}


@app.post("/api/auth/lock")
def auth_lock(response: Response, blpl_session: str | None = Cookie(default=None)) -> dict:
    if blpl_session:
        identity.lock(blpl_session)
    response.delete_cookie(_SESSION_COOKIE)
    return {"unlocked": False}


@app.post("/api/auth/change-passphrase")
def auth_change(body: ChangePassphraseBody, _: str = Depends(require_session)) -> dict:
    try:
        identity.change_passphrase(body.old_passphrase, body.new_passphrase)
    except vault.WrongPassphrase:
        raise HTTPException(status_code=401, detail="wrong current passphrase")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


# --------------------------------------------------------------------------
# Settings: LLM priority/models (config) + API keys (vault)
# --------------------------------------------------------------------------


@app.get("/api/settings")
def get_settings(_: str = Depends(require_session)) -> dict:
    cfg = _load_config()
    return {
        "llm_priority": cfg.llm_priority,
        "llm_models": cfg.llm_models,
        "known_providers": list(appconfig.KNOWN_PROVIDERS),
        # Presence and timestamps only — the values never leave the vault.
        "secrets": [{"provider": m.provider, "updated_at": m.updated_at} for m in identity.list_secrets()],
    }


class LlmSettingsBody(BaseModel):
    priority: list[str]
    models: dict[str, str]


@app.put("/api/settings/llm")
def put_llm_settings(body: LlmSettingsBody, _: str = Depends(require_session)) -> dict:
    cfg = _load_config()
    cfg.llm_priority = body.priority
    cfg.llm_models = {**cfg.llm_models, **body.models}
    try:
        appconfig.save(CONFIG_PATH, cfg)
    except ValueError as exc:  # unknown provider, empty priority
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


class SecretBody(BaseModel):
    value: str


@app.put("/api/settings/secrets/{provider}")
def put_secret(provider: str, body: SecretBody, token: str = Depends(require_session)) -> dict:
    try:
        identity.set_secret(token, provider, body.value)
    except (ValueError, Locked) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "provider": provider}


@app.delete("/api/settings/secrets/{provider}")
def delete_secret(provider: str, _: str = Depends(require_session)) -> dict:
    removed = identity.delete_secret(provider)
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------


def _project_dir(project_id: str) -> Path:
    try:
        d = projects.project_dir(project_id)
    except ProjectError:
        raise HTTPException(status_code=400, detail="invalid project id")
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    return d


def _latest(pipeline_dir: Path, suffix: str) -> Path | None:
    """The current emitted file of a kind, or the newest archived one.

    Emitted names embed a UTC stamp (``…_2026-07-14_141235Z.kicad_sch``), so a
    reverse name sort is a recency sort. The non-obvious part is the fallback:
    Stage 6 rotates previous outputs into ``.pipeline/archive/``, and a project
    whose last run was rotated has *every* board one level down. A non-recursive
    glob then reports "no board" for a project holding nine revisions of one —
    which is what dev.04 did. Prefer the live output; fall back to the archive
    rather than claim the board doesn't exist. Callers that care about the
    difference should use ``_latest_with_origin``.
    """
    found, _ = _latest_with_origin(pipeline_dir, suffix)
    return found


def _latest_with_origin(pipeline_dir: Path, suffix: str) -> tuple[Path | None, bool]:
    """``(path, is_archived)`` — is_archived is True when only a rotated copy exists."""
    if not pipeline_dir.is_dir():
        return None, False
    live = sorted(pipeline_dir.glob(f"*{suffix}"), reverse=True)
    if live:
        return live[0], False
    # rglob so any rotation layout is caught, not just archive/ specifically.
    archived = sorted(pipeline_dir.rglob(f"*{suffix}"), reverse=True)
    return (archived[0], True) if archived else (None, False)


def _fab_readiness(pipeline_dir: Path) -> dict | None:
    """A one-glance verdict from the latest Stage 8 review, or None if not run.

    Surfaces the state that must never be missed — placeholder parts and emitter
    defects — at the project level, so 'this board is not fabricable' is visible
    without opening it. Best-effort: a missing or unparseable report is just None.
    """
    report = pipeline_dir / "review_report.json"
    if not report.is_file():
        return None
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    s = data.get("summary") or {}
    placeholders = int(s.get("placeholders", 0))
    emitter = int(s.get("emitter", 0))
    return {
        "placeholders": placeholders,
        "emitter_defects": emitter,
        "blocked": placeholders > 0 or emitter > 0,
    }


@app.get("/api/projects")
def list_projects(_: str = Depends(require_session)) -> list[dict]:
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
                "is_git": (d / ".git").is_dir(),
                "fab": _fab_readiness(pipeline),
            }
        )
    return out


class CloneBody(BaseModel):
    name: str
    remote: str
    branch: str = "main"


@app.post("/api/projects/clone")
def clone_project(body: CloneBody, _: str = Depends(require_session)) -> dict:
    """Clone a remote into a new working copy, and register it in blpl.toml so the
    server remembers where it came from."""
    try:
        projects.clone(body.name, body.remote, body.branch)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cfg = _load_config()
    cfg.projects[body.name] = ProjectEntry(name=body.name, remote=body.remote, branch=body.branch)
    appconfig.save(CONFIG_PATH, cfg)
    return {"ok": True, "id": body.name}


class InitBody(BaseModel):
    name: str


@app.post("/api/projects/init")
def init_project(body: InitBody, _: str = Depends(require_session)) -> dict:
    """Create a new, empty, local git project. A remote can be attached later."""
    try:
        projects.init_local(body.name)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cfg = _load_config()
    cfg.projects[body.name] = ProjectEntry(name=body.name)
    appconfig.save(CONFIG_PATH, cfg)
    return {"ok": True, "id": body.name}


@app.post("/api/projects/import")
async def import_project(
    name: str = Form(...),
    files: list[UploadFile] = File(...),
    _: str = Depends(require_session),
) -> dict:
    """Bring an existing design into the app from the browser: a selection of
    files, or a single .zip of the project folder. The server creates a local
    git working copy, writes the accepted files, and makes the import the first
    commit — so the imported state is always recoverable. What was *not*
    accepted comes back in ``skipped``; nothing is dropped silently.

    Path rules, suffix policy, and folder flattening live in app/importer.py.
    """
    payloads: list[tuple[str, bytes]] = []
    total = 0
    for up in files:
        data = await up.read()
        total += len(data)
        if total > importer.MAX_TOTAL_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"upload exceeds {importer.MAX_TOTAL_BYTES // (1024 * 1024)} MB",
            )
        payloads.append((up.filename or "", data))

    try:
        imported, skipped = importer.collect(payloads)
    except importer.ImportRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    skipped_out = [{"name": s.name, "reason": s.reason} for s in skipped]
    if not imported:
        raise HTTPException(
            status_code=400,
            detail="nothing importable in the upload: "
            + ("; ".join(f"{s.name} ({s.reason})" for s in skipped) or "no files"),
        )

    try:
        dest = projects.init_local(name)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        for f in imported:
            target = dest / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.data)
        projects.commit_all(name, f"Import {len(imported)} files via app upload")
    except Exception:
        # A half-written project would show up in every listing looking real.
        # Roll the directory back and let the error surface.
        shutil.rmtree(dest, ignore_errors=True)
        raise

    cfg = _load_config()
    cfg.projects[name] = ProjectEntry(name=name)
    appconfig.save(CONFIG_PATH, cfg)
    return {
        "ok": True,
        "id": name,
        "imported": len(imported),
        "files": [str(f.path) for f in imported],
        "skipped": skipped_out,
    }


@app.get("/api/projects/{project_id}/git/status")
def git_status(project_id: str, _: str = Depends(require_session)) -> dict:
    _project_dir(project_id)
    try:
        st = projects.status(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "branch": st.branch, "ahead": st.ahead, "behind": st.behind,
        "dirty": st.dirty, "has_remote": st.has_remote,
    }


class CommitBody(BaseModel):
    message: str


@app.post("/api/projects/{project_id}/git/commit")
def git_commit(project_id: str, body: CommitBody, _: str = Depends(require_session)) -> dict:
    _project_dir(project_id)
    try:
        out = projects.commit_all(project_id, body.message)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "committed": out is not None}


@app.get("/api/projects/{project_id}/git/diff")
def git_diff(project_id: str, _: str = Depends(require_session)) -> dict:
    """What changed in the working copy since the last commit — the 'what did that
    run do' view. Diffs are size-capped server-side."""
    _project_dir(project_id)
    try:
        return projects.diff(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/projects/{project_id}/git/pull")
def git_pull(project_id: str, _: str = Depends(require_session)) -> dict:
    _project_dir(project_id)
    try:
        out = projects.pull(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "output": out}


@app.post("/api/projects/{project_id}/git/push")
def git_push(project_id: str, _: str = Depends(require_session)) -> dict:
    _project_dir(project_id)
    try:
        out = projects.push(project_id)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "output": out}


# The files a user edits in the browser: design markdown and the project config.
# Deliberately NOT everything — generated artifacts live in .pipeline/ and are
# read through the artifacts endpoint, and .git is off-limits. Editing is scoped
# to the design inputs.
_EDITABLE_SUFFIXES = {".md", ".yaml", ".yml"}


def _editable_file(project_id: str, name: str) -> Path:
    """Resolve a filename to an editable file directly under the project root.

    The name comes from the client, so it is held to three rules: a bare filename
    with no path separators, an editable suffix, and — after resolving — a parent
    that is exactly the project root. That last check is what stops ``foo/../..``
    or a symlink from reaching outside the design inputs.
    """
    proj = _project_dir(project_id)
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    target = (proj / name).resolve()
    if target.parent != proj.resolve() or target.suffix.lower() not in _EDITABLE_SUFFIXES:
        raise HTTPException(status_code=400, detail="invalid filename")
    return target


class FileBody(BaseModel):
    content: str


@app.get("/api/projects/{project_id}/files")
def list_files(project_id: str, _: str = Depends(require_session)) -> list[dict]:
    """The editable design inputs in the project root, markdown and config."""
    proj = _project_dir(project_id)
    out = []
    for f in sorted(proj.iterdir()):
        if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in _EDITABLE_SUFFIXES:
            out.append({"name": f.name, "bytes": f.stat().st_size})
    return out


@app.get("/api/projects/{project_id}/files/{name}")
def read_file(project_id: str, name: str, _: str = Depends(require_session)):
    target = _editable_file(project_id, name)
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"no file {name!r}")
    return {"name": name, "content": target.read_text(encoding="utf-8", errors="replace")}


@app.put("/api/projects/{project_id}/files/{name}")
def write_file(project_id: str, name: str, body: FileBody, _: str = Depends(require_session)) -> dict:
    """Create or overwrite an editable file. Creating is intended: a fresh project
    is empty, and this is how the first design doc gets written."""
    target = _editable_file(project_id, name)
    target.write_text(body.content, encoding="utf-8")
    return {"ok": True, "name": name, "bytes": target.stat().st_size}


@app.get("/api/projects/{project_id}/artifacts/{name}")
def read_artifact(project_id: str, name: str, _: str = Depends(require_session)):
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
def design_sources(project_id: str, _: str = Depends(require_session)) -> dict:
    proj = _project_dir(project_id)
    pipeline = proj / ".pipeline"
    sources = []
    archived = False
    for suffix in (".kicad_sch", ".kicad_pcb"):
        f, was_archived = _latest_with_origin(pipeline, suffix)
        if f is not None:
            archived = archived or was_archived
            sources.append(
                {"filename": f.name, "content": f.read_text(encoding="utf-8", errors="replace")}
            )
    if not sources:
        raise HTTPException(status_code=404, detail="no emitted KiCad files — run stage6 first")
    # The viewer badges this: showing a rotated board without saying so would be
    # the worst outcome — you would review a revision you are not about to fab.
    return {"sources": sources, "archived": archived}


# --------------------------------------------------------------------------
# References, sandbox, and conversations
#
# Ported from the second backend that used to live in blpl/webapp/. That app
# served this same React bundle against an API with no auth, no git, and no
# /design — so the UI's very first call (/api/auth/status) 404'd and the whole
# thing dead-ended. There is now one backend; these are the routes it was
# missing. Everything here sits behind require_session like the rest of /api.
# --------------------------------------------------------------------------


def _blpl_dir(project_id: str) -> Path:
    return _project_dir(project_id) / ".blpl"


def _references_path(project_id: str) -> Path:
    return _blpl_dir(project_id) / "references.json"


def _conversations_dir(project_id: str) -> Path:
    return _blpl_dir(project_id) / "conversations"


def _load_manifest(project_id: str) -> ReferenceManifest:
    """The project's reference manifest, or an empty one rooted at the project.

    A corrupt manifest degrades to empty rather than 500ing the whole project:
    an unreadable references.json should cost you your external references, not
    access to your board.
    """
    proj = _project_dir(project_id)
    path = _references_path(project_id)
    if path.is_file():
        try:
            return ReferenceManifest.load(path)
        except Exception:
            pass
    return ReferenceManifest.empty(project_id=project_id, workspace_root=proj)


def _sandbox_for(project_id: str) -> FilesystemSandbox:
    return FilesystemSandbox(
        manifest=_load_manifest(project_id),
        global_allowlist=load_global_allowlist(),
        global_denylist=load_global_denylist(),
    )


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
def get_references(project_id: str, _: str = Depends(require_session)) -> dict:
    manifest = _load_manifest(project_id)
    return {
        "project_id": project_id,
        "workspace_root": str(manifest.workspace_root),
        "references": [r.to_dict() for r in manifest.references],
    }


@app.put("/api/projects/{project_id}/references")
def put_references(
    project_id: str, payload: ReferenceManifestInput, _: str = Depends(require_session)
) -> dict:
    manifest = _load_manifest(project_id)
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
    manifest.references = refs
    _conversations_dir(project_id).mkdir(parents=True, exist_ok=True)
    path = _references_path(project_id)
    manifest.save(path)
    return {"references": [r.to_dict() for r in refs], "saved_to": str(path)}


@app.get("/api/projects/{project_id}/sandbox")
def sandbox_summary(project_id: str, _: str = Depends(require_session)) -> dict:
    return _sandbox_for(project_id).summary()


def _artifact_meta(path: Path, pipeline: Path) -> dict:
    """Name, size, and creation time as UTC ``YYYY-MM-DD_HHMMSSZ``."""
    st = path.stat()
    ts = getattr(st, "st_birthtime", None) or st.st_mtime
    return {
        "name": str(path.relative_to(pipeline)),
        "size": st.st_size,
        "created": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d_%H%M%SZ"),
    }


@app.get("/api/projects/{project_id}/artifacts")
def list_artifacts(project_id: str, _: str = Depends(require_session)) -> dict:
    """Every artifact the pipeline has written, newest first.

    Top-level only. Rotated copies under archive/ are deliberately excluded —
    listing nine historical boards alongside the current one is how the artifact
    list stops being useful. /design already falls back to the archive when it
    has to, and says so.
    """
    pipeline = _project_dir(project_id) / ".pipeline"
    if not pipeline.is_dir():
        return {"artifacts": []}
    artifacts = [_artifact_meta(p, pipeline) for p in sorted(pipeline.iterdir()) if p.is_file()]
    artifacts.sort(key=lambda a: a["created"], reverse=True)
    return {"artifacts": artifacts}


class NewConversationInput(BaseModel):
    title: str = "conversation"


class MessageInput(BaseModel):
    role: str
    content: str
    metadata: dict | None = None


@app.get("/api/projects/{project_id}/conversations")
def get_conversations(project_id: str, _: str = Depends(require_session)) -> list[dict]:
    return [m.to_dict() for m in list_conversations(_conversations_dir(project_id))]


@app.post("/api/projects/{project_id}/conversations")
def create_conversation(
    project_id: str, payload: NewConversationInput, _: str = Depends(require_session)
) -> dict:
    d = _conversations_dir(project_id)
    d.mkdir(parents=True, exist_ok=True)
    conv = Conversation.create(d, title=payload.title)
    return {"slug": conv.slug, "filename": conv.path.name, "started_at": conv.started_at}


@app.get("/api/projects/{project_id}/conversations/{filename}")
def read_conversation(project_id: str, filename: str, _: str = Depends(require_session)) -> dict:
    try:
        conv = Conversation.open_existing(_conversations_dir(project_id), filename)
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
    project_id: str, filename: str, payload: MessageInput, _: str = Depends(require_session)
) -> dict:
    try:
        conv = Conversation.open_existing(_conversations_dir(project_id), filename)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return conv.append(payload.role, payload.content, payload.metadata)


# --------------------------------------------------------------------------
# Stage execution
# --------------------------------------------------------------------------


def _stage_env(token: str, stage_name: str) -> dict[str, str]:
    """The subprocess environment for a stage run.

    For LLM stages, resolve the *whole* fallback chain the config prefers and has
    keys for, inject every provider's key into its own SDK env var, and hand the
    ordered chain to the pipeline as HDM_LLM_CHAIN. The pipeline then tries each in
    turn, so a dead key or an unreachable provider fails over to the next instead
    of failing the stage. This is where the vault meets the pipeline: keys are
    decrypted here, passed in the child's environment, and never written to a
    command line or a log. Deterministic stages get the base environment unchanged.
    """
    env = dict(os.environ)
    if stage_name not in _LLM_STAGES:
        return env
    return _inject_llm_env(env, token)


def _inject_llm_env(env: dict[str, str], token: str) -> dict[str, str]:
    """Add the resolved LLM fallback chain and its keys to an environment.

    Keys are decrypted here and passed only in the child's environment, never on a
    command line or in a log. Raises NoUsableProvider if nothing configured has a
    key, which the caller turns into a clear 400.
    """
    cfg = _load_config()
    chain = llm_resolver.resolve_chain(cfg, identity.providers_with_keys())
    if not chain:
        llm_resolver.resolve_primary(cfg, identity.providers_with_keys())  # raise the good message

    for rp in chain:
        env_var = _PROVIDER_ENV.get(rp.provider)
        if env_var:
            key = identity.get_secret(token, rp.provider)
            if key is not None:
                env[env_var] = key

    env["HDM_LLM_CHAIN"] = json.dumps([{"provider": rp.provider, "model": rp.model} for rp in chain])
    env["HDM_LLM_PROVIDER"] = chain[0].provider
    if chain[0].model:
        env["HDM_LLM_MODEL"] = chain[0].model
    return env


def _stream_subprocess(cmd: list[str], env: dict[str, str]) -> StreamingResponse:
    """Run a command and stream its combined output to the browser as SSE.

    Shared by single-stage and whole-pipeline runs. The argv is echoed in the
    start event — safe, because keys ride in the environment, never on the
    command line. If the client disconnects, the generator is closed and the
    child is killed rather than left orphaned.
    """

    async def events():
        yield f"event: start\ndata: {json.dumps({'cmd': cmd})}\n\n"
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
        )
        try:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                yield f"event: log\ndata: {json.dumps({'line': line})}\n\n"
            code = await proc.wait()
            yield f"event: done\ndata: {json.dumps({'exit_code': code})}\n\n"
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/projects/{project_id}/stages/{stage_name}")
async def run_stage(
    project_id: str, stage_name: str, token: str = Depends(require_session)
) -> StreamingResponse:
    """Run a pipeline stage, streaming its output to the browser as it happens."""
    if stage_name not in VALID_STAGES:
        raise HTTPException(status_code=400, detail=f"unknown stage {stage_name!r}")
    proj = _project_dir(project_id)
    try:
        env = _stage_env(token, stage_name)
    except llm_resolver.NoUsableProvider as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cmd = [sys.executable, "-m", "blpl.core.cli", stage_name, "--project-dir", str(proj)]
    return _stream_subprocess(cmd, env)


# The stages the whole-pipeline runner understands, in order. The CLI `run`
# command drives 0→8; this is the allowlist the UI's from/to must fall within.
_PIPELINE_STAGES = [
    "stage0", "stage1", "stage2", "stage3", "stage4",
    "stage5", "stage6", "stage7", "stage8",
]


@app.post("/api/projects/{project_id}/pipeline")
async def run_pipeline(
    project_id: str,
    from_stage: str = "stage0",
    to_stage: str = "stage8",
    token: str = Depends(require_session),
) -> StreamingResponse:
    """Run a contiguous range of stages end-to-end, streaming per-stage progress.

    Wraps `blpl run --from … --to …`, which prints a header per stage and keeps
    going past a failure (--continue-on-error) so one stage's error doesn't hide
    the rest. stage1 uses an LLM, so the chain + keys are injected whenever the
    range reaches it; a pure stage5→8 range needs no key and won't be blocked for
    lack of one.
    """
    if from_stage not in _PIPELINE_STAGES or to_stage not in _PIPELINE_STAGES:
        raise HTTPException(status_code=400, detail="from/to must be stage0…stage8")
    if _PIPELINE_STAGES.index(from_stage) > _PIPELINE_STAGES.index(to_stage):
        raise HTTPException(status_code=400, detail="from stage is after to stage")
    proj = _project_dir(project_id)

    env = dict(os.environ)
    lo, hi = _PIPELINE_STAGES.index(from_stage), _PIPELINE_STAGES.index(to_stage)
    if lo <= _PIPELINE_STAGES.index("stage1") <= hi:
        try:
            env = _inject_llm_env(env, token)
        except llm_resolver.NoUsableProvider as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    cmd = [
        sys.executable, "-m", "blpl.core.cli", "run",
        "--project-dir", str(proj),
        "--from", from_stage, "--to", to_stage,
        "--continue-on-error",
    ]
    return _stream_subprocess(cmd, env)


# --------------------------------------------------------------------------
# Optional SPA hosting (local `blpl serve` only)
#
# In the container nginx serves the built frontend and proxies /api here, so
# this mount finds nothing and does nothing — the backend image contains no
# dist/. Locally there is no nginx, so `blpl serve` would otherwise hand you a
# bare API. Mounting the build here keeps one-port local serving, which is the
# ergonomic the old blpl/webapp had and the reason people reached for it.
#
# Registered last so every /api route above wins the match.
# --------------------------------------------------------------------------

# app/backend/app/main.py → app/backend → app → app/frontend/dist.
# Deliberately relative rather than indexed off the repo root: in the container
# this file is /app/app/main.py, a shallower tree, and an absolute parents[N]
# either raises IndexError or silently points somewhere wrong. This form resolves
# to a path that simply does not exist there, so the mount is skipped — which is
# correct, because nginx serves the frontend in the container.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_STATIC_DIR = Path(
    os.environ.get("BLPL_STATIC_DIR") or _BACKEND_ROOT.parent / "frontend" / "dist"
)


# Registered after every real /api route (so those win) and unconditionally —
# NOT inside the `if static exists` branch. Whether a frontend happens to be
# built must not change API semantics. Without this, the GET-only SPA catch-all
# below matches an unknown /api path for routing purposes but rejects the method,
# turning what should be 404 into 405 for every POST/PUT/DELETE.
@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def _api_route_not_found(rest: str):
    raise HTTPException(status_code=404, detail=f"no API route /api/{rest}")


if (_STATIC_DIR / "index.html").is_file():
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    if (_STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        """index.html for any non-API path — client-side routing handles the rest.

        /api is already claimed by _api_route_not_found above; this guard is the
        belt to that suspenders, so a reordering never serves the SPA shell in
        place of an API error.
        """
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail=f"no API route /{full_path}")
        candidate = (_STATIC_DIR / full_path).resolve()
        if (
            full_path
            and candidate.is_file()
            and candidate.is_relative_to(_STATIC_DIR.resolve())
        ):
            return FileResponse(candidate)
        return FileResponse(_STATIC_DIR / "index.html")
