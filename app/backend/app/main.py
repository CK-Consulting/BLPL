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
import subprocess
import sys
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from . import appconfig, llm_resolver, vault
from .appconfig import AppConfig, ProjectEntry
from .identity import Identity, Locked
from .projects import ProjectError, Projects
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


def _set_session_cookie(response: Response, token: str) -> None:
    # httpOnly so page JS can never read the token; SameSite=Lax so it rides
    # normal navigation but not cross-site POSTs. Not Secure-flagged here because
    # the deploy terminates TLS at a reverse proxy on the same origin; set
    # BLPL_COOKIE_SECURE=1 when the app is served over https directly.
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=bool(os.environ.get("BLPL_COOKIE_SECURE")),
        max_age=8 * 3600,
    )


@app.post("/api/auth/initialize")
def auth_initialize(body: PassphraseBody, response: Response) -> dict:
    """First-run: set the passphrase. Refused if one already exists."""
    try:
        token = identity.initialize(body.passphrase)
    except Exception as exc:  # AlreadyInitialized / ValueError
        raise HTTPException(status_code=400, detail=str(exc))
    _set_session_cookie(response, token)
    return {"unlocked": True}


@app.post("/api/auth/unlock")
def auth_unlock(body: PassphraseBody, response: Response) -> dict:
    try:
        token = identity.unlock(body.passphrase)
    except vault.WrongPassphrase:
        raise HTTPException(status_code=401, detail="wrong passphrase")
    except Exception as exc:  # NotInitialized
        raise HTTPException(status_code=400, detail=str(exc))
    _set_session_cookie(response, token)
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
    if not pipeline_dir.is_dir():
        return None
    files = sorted(pipeline_dir.glob(f"*{suffix}"), reverse=True)
    return files[0] if files else None


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
    for suffix in (".kicad_sch", ".kicad_pcb"):
        f = _latest(pipeline, suffix)
        if f is not None:
            sources.append(
                {"filename": f.name, "content": f.read_text(encoding="utf-8", errors="replace")}
            )
    if not sources:
        raise HTTPException(status_code=404, detail="no emitted KiCad files — run stage6 first")
    return {"sources": sources}


# --------------------------------------------------------------------------
# Stage execution
# --------------------------------------------------------------------------


def _stage_env(token: str, stage_name: str) -> dict[str, str]:
    """The subprocess environment for a stage run.

    For LLM stages, resolve the provider the config prefers *and* has a key for,
    then inject that one key plus HDM_LLM_PROVIDER/HDM_LLM_MODEL. This is where the
    vault meets the pipeline: the key is decrypted here, handed to the child in
    its environment, and never written down. Deterministic stages get the base
    environment unchanged.
    """
    env = dict(os.environ)
    if stage_name not in _LLM_STAGES:
        return env

    cfg = _load_config()
    primary = llm_resolver.resolve_primary(cfg, identity.providers_with_keys())
    env["HDM_LLM_PROVIDER"] = primary.provider
    if primary.model:
        env["HDM_LLM_MODEL"] = primary.model
    env_var = _PROVIDER_ENV.get(primary.provider)
    if env_var:
        key = identity.get_secret(token, primary.provider)
        if key is None:
            raise HTTPException(status_code=400, detail=f"no key stored for {primary.provider}")
        env[env_var] = key
    return env


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

    async def events():
        cmd = [sys.executable, "-m", "blpl.core.cli", stage_name, "--project-dir", str(proj)]
        # The env carries a decrypted key; the command line never does, so it is
        # safe to echo the argv to the client.
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
