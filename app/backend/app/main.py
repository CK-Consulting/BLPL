"""BLPL app backend — generate, review, and see a board from any workstation.

Three things make this the hosted app rather than the earlier vertical slice:

* KiCad lives in the *image*, not on your laptop (FROM kicad/kicad:10.0.0), so
  every workstation you sit at renders and exports identically.

* Clerk is the front door. Every route under /api except the health probe and
  the config probe requires a Clerk session token, verified here against Clerk's
  published JWKS (app/clerk_auth.py) — the backend gets no Clerk SDK, so it does
  the verification itself, and pins the issuer because a correctly-signed token
  from someone else's Clerk instance is otherwise indistinguishable from ours.

* Provider API keys belong to a *user*, not to the server. They live in Postgres
  sealed with AES-GCM under a server-held key (app/keystore.py), and there is no
  environment fallback: a shared key would mean every user of a deployment
  spending the operator's quota on the operator's account. Be clear about what
  this protects — a stolen dump is inert, a dump plus the server key is not, and
  the operator can always read them. A user typing a key into Settings is
  trusting the operator, not only the software.

* Your projects are git-backed working copies on the server, so they follow you
  between machines. See app/projects.py.

The security posture in one line: everything under /api requires an unlocked
session except the auth handshake and the health probe. A server restart drops
all sessions (the decrypted key is only ever in RAM), and unlocking again is the
way back in.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import urllib.request
import shutil
import subprocess
import sys
import logging
from collections.abc import Sequence
from contextlib import asynccontextmanager, contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import (
    BackgroundTasks,
    Cookie,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
# Annotations are lazy (from __future__), which hid that these were never
# imported — Session only ever appeared in a type hint. select() is a runtime
# call and would have raised NameError on the first clashing project name.
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import (
    activity,
    appconfig,
    attachments,
    autopush,
    board_config,
    chat as chat_mod,
    clerk_auth,
    components,
    egress,
    gitidentity,
    grants,
    idlepolicy,
    importer,
    keystore,
    llm_ignore,
    passkeys,
    pdf_verdicts,
    llmconfig,
    llm_resolver,
    mailer,
    openstate,
    projectacl,
    projectkey,
    profile as profile_mod,
    providers as provider_catalog,
    runqueue,
    runs,
    serverkey,
    unlock as unlock_mod,
    userkey,
    users,
    workspace,
    workspace_files,
    worktrees,
)
from blpl.core import schema as hdm_schema
from .appconfig import AppConfig
from .db import SessionFactory, session_scope
from .models import Project, ProjectInvitation, ProjectPolicy, Run, User
from .sandbox.gate import GateBusy as _GateBusy
from blpl.core import library_upload, limits, project_manifest, quarantine
from . import conversations as conversations_mod
from .conversations import Conversation, list_conversations
from .projects import ProjectError, Projects, check_remote_url
from .vault import VaultError
from .references import (
    FilesystemSandbox,
    Reference,
    ReferenceManifest,
    ReferencePolicyError,
    load_global_allowlist,
    load_global_denylist,
    validate_reference,
)

logger = logging.getLogger("blpl.app")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Make sure the key that seals provider keys exists before anyone stores one.

    Created at boot rather than lazily on first write so the operator learns
    where it is — and can get it into their backup story — *before* it is the
    only thing standing between a database dump and every user's keys. Losing it
    later means every stored key is unreadable and must be re-entered.
    """
    # First, before anything reads a project: a backend that was told it has
    # no project mount but can see one refuses to start (MountMisconfigured).
    mount_warning = workspace_files.check_project_mount(PROJECTS_ROOT)
    if mount_warning:
        logger.warning("!" * 72)
        logger.warning("PROJECT MOUNT: %s", mount_warning)
        logger.warning("!" * 72)
    _migrate_sealed_blobs()
    # Squid's questions about workspace egress (egress_server.py), when the
    # deployment runs workspaces behind it.
    egress_decisions = None
    sock = os.environ.get("BLPL_EGRESS_SOCKET")
    if sock and (egress.enforced() or workspace_files.enabled()):
        from . import egress_server

        Path(sock).parent.mkdir(parents=True, exist_ok=True)
        egress_decisions = egress_server.serve(sock, egress_server.db_decide)
    key_path = serverkey.key_path(_DATA)
    existed = serverkey.load(_DATA) is not None
    serverkey.load_or_create(_DATA)
    if not existed and not os.environ.get("BLPL_SERVER_KEY"):
        logger.warning(
            "generated a new server key at %s — it seals every stored provider key. "
            "Back it up separately from the database; together they are the lock and its key.",
            key_path,
        )
    # Before the sweeper runs, so its first pass already sees anything the
    # last process left open.
    _restore_open_workspaces()
    sweeper = asyncio.create_task(_seal_idle_workspaces())
    try:
        yield
    finally:
        sweeper.cancel()
        if egress_decisions is not None:
            egress_decisions.shutdown()
            egress_decisions.server_close()


def _migrate_sealed_blobs() -> None:
    """Move blobs out of projects/.sealed, where a backend with the mount can.

    A mountless backend cannot see the old location, so it leaves this to the
    worker, which does the same at its start, or to an operator running
    ``python -m app.workspace migrate-sealed`` before switching layouts."""
    if workspace_files.mount_absent():
        return
    try:
        m = workspace.migrate_sealed(PROJECTS_ROOT)
    except (OSError, workspace.WorkspaceError):
        logger.exception("could not move sealed projects to %s", workspace.sealed_root(PROJECTS_ROOT))
        return
    if m.moved:
        logger.info("moved %d sealed project(s) to %s", len(m.moved), workspace.sealed_root(PROJECTS_ROOT))
    for name in m.conflicts:
        logger.error("sealed project %s has two different blobs; the one in %s is used, "
                     "the other is kept in %s", name, workspace.sealed_root(PROJECTS_ROOT),
                     PROJECTS_ROOT / workspace.SEALED_DIR)


def _server_key() -> bytes:
    return serverkey.load_or_create(_DATA)


def _note_workspace_open(
    session: Session, name: str, path: Path, project_key: bytes, holder: int | None
) -> None:
    """Open a workspace in the registry and write the fact through to the database.

    The registry is what every request reads; the row is what survives a
    restart. A failure to persist must not fail the open — the user gets their
    project either way — but it does mean a crash before the next open would
    leave this one unsealed, so it is logged loudly rather than passed over.
    """
    workspaces.note_open(name, path, project_key, holder)
    try:
        openstate.record_open(session, name, path, project_key, _server_key(), holder)
    except Exception:  # noqa: BLE001 — persistence is best-effort for the caller
        logger.exception(
            "could not record %s as open; a restart would not know to seal it", name
        )


def _touch_workspace(session: Session, name: str, holder: int | None = None) -> None:
    workspaces.touch(name, holder)
    try:
        openstate.touch(session, name, holder)
    except Exception:  # noqa: BLE001
        logger.exception("could not persist activity on %s", name)


def _restore_open_workspaces() -> None:
    """Re-adopt workspaces that were open when this process last stopped.

    Without this the idle sweeper has nothing to sweep after a crash, and a
    project that was open at the time stays plaintext on disk until somebody
    happens to open and lock it again.
    """
    try:
        with SessionFactory() as session:
            restored = openstate.restore(
                session, _server_key(), PROJECTS_ROOT,
                sees_projects=not workspace_files.mount_absent())
            for name, path, key, touched, holders in restored:
                workspaces.adopt(name, path, key, touched, holders)
            session.commit()
    except Exception:  # noqa: BLE001 — a failed restore must not stop the app
        logger.exception("could not restore open workspaces")
        return
    if restored:
        logger.info(
            "restored %d open workspace(s) from the last run: %s",
            len(restored),
            ", ".join(name for name, *_ in restored),
        )


async def _seal_idle_workspaces() -> None:
    """Seal workspaces nobody has touched for a while.

    The other half of the answer to "when does it re-seal". Locking covers the
    people who say they are done; this covers the far more common case of
    closing a laptop without saying anything, which would otherwise leave a
    project decrypted until someone came back to it.

    Runs here rather than in a worker because the key lives here: an open
    workspace's key is held in this process, and a worker has no way to obtain
    one.
    """
    while True:
        try:
            await asyncio.sleep(60)
            if workspace_files.enabled():
                # Workspaces the agent reports idle are stopped and sealed.
                with SessionFactory() as session:
                    await workspace_files.sweep_and_seal(
                        lambda name, gate_held=False: _seal_workspace(
                            session, name, gate_held=gate_held,
                            reason=f"idle {idlepolicy.minutes_for(session, name)} min "
                                   "(no file, git or run activity)"),
                        limit_s=lambda name: 60 * idlepolicy.minutes_for(session, name))
                    session.commit()
            _seal_idle_registry()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a sweep that fails must not stop the next
            logger.exception("idle workspace sweep failed")


def _seal_idle_registry() -> list[str]:
    """Seal every open workspace idle past its project's own limit.

    Not named after the sweeper above, which it once shared a name with: the
    later definition replaced the coroutine the lifespan starts, and the
    backend failed to boot (Codex, PR #47)."""
    sealed = []
    with SessionFactory() as session:
        now = datetime.now(timezone.utc)
        for entry in workspaces.all_open():
            limit = idlepolicy.minutes_for(session, entry.workspace)
            idle_min = (now - entry.last_touched).total_seconds() / 60
            if idle_min >= limit and _seal_workspace(
                    session, entry.workspace, reason=f"idle {idle_min:.0f} min (limit {limit})"):
                sealed.append(entry.workspace)
        session.commit()
    return sealed


app = FastAPI(title="BLPL", version="0.5.0", lifespan=_lifespan)


@app.middleware("http")
async def _never_cache(request, call_next):
    """No API response is ever cached, by anything, for any length of time.

    Everything this server returns is the state of a project *right now* — a
    file being edited, a run's log, the tree, a proposal. None of it is worth
    keeping and all of it goes wrong when it is kept.

    Per-endpoint headers were the obvious fix and the wrong shape. `FileResponse`
    sets last-modified and an etag but no Cache-Control, and a response carrying
    a validator with no freshness directive is one a browser may hold as fresh on
    its own account — heuristically, about a tenth of the file's age — answering
    a fetch from cache without asking the server anything. That is how a pane
    kept showing a design document that no longer existed on disk, through a
    refetch that was working perfectly. Any endpoint added later would have had
    the same hole, so the rule belongs here rather than in each of them.
    """
    response = await call_next(request)
    # Everything, not only /api/. In `blpl serve` there is no nginx: this app
    # serves index.html and the fingerprinted bundle itself, and a response with
    # a validator and no freshness directive is one the browser may hold on its
    # own account — so a rebuilt frontend kept loading the old bundle locally,
    # which is exactly the failure the nginx rules were written to stop.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    # And nothing is ever sniffed into a type it was not sent as. Several
    # routes return project text (artifacts, run logs, the release zip), and a
    # browser that guessed HTML for one would run it on this origin.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


# State roots. All three are volumes on the deploy; all three default under one
# data dir so a bare `docker run` still works.
_DATA = Path(os.environ.get("BLPL_DATA_ROOT", "/app/data"))
PROJECTS_ROOT = Path(os.environ.get("BLPL_PROJECTS_ROOT", str(_DATA / "projects"))).resolve()
CONFIG_PATH = Path(os.environ.get("BLPL_CONFIG", str(_DATA / "blpl.toml")))

projects = Projects(PROJECTS_ROOT)
# Unlocked master keys, one per Clerk session. In-memory on purpose: a
# restart drops them, which is the correct behaviour for derived key
# material — see app/unlock.py.
unlocked = unlock_mod.Unlocked()
# Which project workspaces are currently unsealed, and the key that will seal
# them again. In this process because the key must be: see app/workspace.py.
workspaces = workspace.Registry()
# Run history is plaintext facts-about-what-happened, deliberately NOT in
# vault.db — that file's contract is "a dump of it is a dump of ciphertext".
# Kept only for chat tool-call history, which is written by the API process
# itself and is not part of the run queue. Stage execution moved to
# app/runqueue.py and the worker container; this no longer starts anything.
run_manager = runs.RunManager(_DATA / "runs.db", _DATA / "runs")
# Chat turns are in-process and in-memory: a turn needs sub-second first tokens
# and (soon) approval round-trips, neither of which survives a pipe. What is
# durable is the conversation JSONL each turn writes to.
chat_sessions = chat_mod.ChatSessionManager()

# Environment variable each provider's SDK reads its key from. ollama is keyless.
_PROVIDER_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


# --------------------------------------------------------------------------
# Where an endpoint's key comes from
#
# One place: the signed-in user's own row. There is deliberately no fallback to
# the server's environment. A shared ANTHROPIC_API_KEY would mean every user of
# this deployment silently spending the operator's quota on the operator's
# account — a default nobody can consent to, and one that gets more wrong the
# more people use the server. Bring your own key, or route the task somewhere
# keyless: ollama, or an OpenAI-compatible endpoint declaring auth = "none".
#
# The endpoint registry already models this well. A key belongs to a *named
# endpoint*, not a provider kind, so two Anthropic accounts are two endpoints
# with two keys, and a self-hosted OpenAI-compatible server is just another
# endpoint with its own base_url and credential.
# --------------------------------------------------------------------------

VALID_STAGES = {
    "doctor",
    # Not a stage, and runnable for the same reason doctor is: it is
    # deterministic, it reads the markdown that is already here, and doctor
    # tells you to run it. DOC-007 said "Run `blpl init`" while the workbench
    # offered no way to — so the one thing standing between a design and Stage 5
    # could only be done by finding a shell.
    "init",
    "stage0-det", "stage0-llm", "stage0-compare",
    "stage1", "stage1-synthesize-connectors",
    "stage2", "stage3", "stage4", "stage5",
    "stage6", "stage6-plugin",
    # The autoroute, under the stage name `blpl run` gives it. Runnable alone so
    # a board emitted once can be routed again without re-emitting it.
    "stage6a",
    "stage7", "stage8",
}

# Stages that call an LLM. Only these need a provider key injected; the rest are
# deterministic and run without one.
_LLM_STAGES = {"stage0-llm", "stage0-compare", "stage1", "stage1-synthesize-connectors"}


# --------------------------------------------------------------------------
# Session gate
# --------------------------------------------------------------------------


def require_user(
    request: Request,
    session: Session = Depends(session_scope),
    authorization: str | None = Header(default=None),
    __session: str | None = Cookie(default=None, alias="__session"),
) -> User:
    """FastAPI dependency: the request must carry a verified Clerk session.

    Returns *our* user row, not Clerk's claims, so every handler downstream
    works in terms of an owner it can attach data to. The row is created on
    first sign-in; Clerk has already decided the person may authenticate, so
    there is no second approval step here.

    Both places Clerk puts a token are accepted — the Authorization header the
    frontend sends after getToken(), and the __session cookie on a same-origin
    request. FastAPI caches dependencies per request, so the session opened here
    is the same one the handler receives.

    401 for every failure, with the reason logged rather than returned: telling
    an unauthenticated caller *why* verification failed is free reconnaissance.
    """
    if not clerk_auth.configured():
        # A 503 rather than a 401: nobody can fix this by signing in again, and
        # a login screen that cannot possibly work is worse than an error.
        raise HTTPException(
            status_code=503,
            detail="this server has no Clerk issuer configured; set BLPL_CLERK_ISSUER",
        )
    try:
        who = clerk_auth.verify(clerk_auth.token_from_request(authorization, __session))
    except clerk_auth.ClerkAuthError as exc:
        logger.info("rejected a request: %s", exc)
        raise HTTPException(status_code=401, detail="not signed in")
    user = users.get_or_create(session, who)
    # Stashed for the dependencies below, which need the session id to find this
    # user's unlocked key but must not re-verify the token to get it.
    request.state.clerk_claims = who.claims
    return user


def require_onboarded(user: User = Depends(require_user)) -> User:
    """The gate for everything except onboarding itself.

    A 428 rather than a 403: the request is not forbidden, it is premature, and
    the client's job is to send the user to setup rather than to show an error.
    Using 403 here would be indistinguishable from a permissions problem once
    project sharing lands.
    """
    if not profile_mod.is_complete(user):
        raise HTTPException(status_code=428, detail="finish setting up your account first")
    return user


def require_master_key(request: Request, user: User = Depends(require_user)) -> bytes:
    """The unlocked master key for this session, or 423 Locked.

    Separate from require_onboarded because most routes never touch a secret —
    listing projects, reading a file, watching a run. Only the ones that seal or
    open a provider key need this, and making them ask for it explicitly is what
    keeps "which endpoints have keys" answerable while locked.
    """
    claims = getattr(request.state, "clerk_claims", {}) or {}
    key = unlocked.get(unlock_mod.session_key_for(user.clerk_user_id, claims))
    if key is None:
        raise HTTPException(status_code=423, detail="locked: enter your encryption passphrase")
    return key


def optional_master_key(request: Request, user: User = Depends(require_user)) -> bytes | None:
    """The unlocked master key if this session has one, else None — never 423.

    For the routes that commit. A commit needs no secret, so they must keep
    working while locked; but a project that pushes after every commit needs
    the user's git credential to do it, and that is sealed under this key. With
    no key the push is recorded as failed, with the reason, rather than the
    save being refused.
    """
    claims = getattr(request.state, "clerk_claims", {}) or {}
    return unlocked.get(unlock_mod.session_key_for(user.clerk_user_id, claims))


def _load_config() -> AppConfig:
    """The install-wide file config: the project registry and the MCP servers.

    NOT the LLM registry. Endpoints and task routes are per-user in Postgres
    (app/llmconfig.py) — reading them from here is the bug where the second
    person to finish setup silently rewrote the first one's routing.
    """
    return appconfig.load(CONFIG_PATH)


# --------------------------------------------------------------------------
# Health + auth (the only routes reachable while locked)
# --------------------------------------------------------------------------


@app.api_route("/api/authz/gate", methods=["GET", "HEAD"])
def authz_gate(
    request: Request,
    session: Session = Depends(session_scope),
    authorization: str | None = Header(default=None),
    __session: str | None = Cookie(default=None, alias="__session"),
) -> Response:
    """A yes/no for nginx, so a proxied app can be put behind this app's sign-in.

    The desktop KiCad has no authentication of its own — it answers 200 and
    hands over a session with the project directory mounted. Serving it at a
    path under this origin does not by itself change that: nginx proxies, it
    does not know what a Clerk session is. `auth_request` is how it asks.

    It works because Clerk's `__session` cookie rides a same-origin request, so
    a top-level navigation to /kicad/ carries credentials that a Bearer token in
    JavaScript would not. Empty body and no cache: the answer is about this
    request, and nginx discards everything but the status.

    HEAD as well as GET, because nginx issues the sub-request with the original
    request's method and the probe deciding whether to show the launch button
    used HEAD — a GET-only route answered 405, which auth_request reads as a
    denial, so the button stayed hidden while the desktop was running.

    And signed in is not enough. Every project route in this app checks
    membership before opening anything; letting any onboarded account through
    would put project files, and a shared desktop session, in front of people
    who are not on them.

    What "membership" means here depends on what is mounted, and there are two
    shapes because a desktop cannot do what an HTTP route does. A route answers
    one request and can hand a different caller different files. This is one
    long-lived Unix session on one filesystem, shared by whoever opens it — two
    people looking at /kicad/ are looking at the same screen. So the gate can
    only ever be all-or-nothing over the mounted set, and the honest thing is to
    make it say so:

      * KICAD_DESKTOP_PROJECT names a project — that one is mounted, so
        membership of it is the question, exactly as before.

      * It is unset (the default, and what a multi-project workspace wants) —
        the whole projects root is mounted, so the question is membership of
        every project in it. Computed per request from the directory rather
        than from a list someone has to remember to update, which means adding
        a project cannot silently widen who may open the desktop.

    For a single-operator deployment the second form is simply "all of mine",
    with nothing to configure. On a shared one it denies anybody who is not on
    all of them — which is not a regression but the truth about a shared
    filesystem, and the reason to name a single project instead.

    Directories with no project row are ignored rather than treated as a denial.
    Nothing but this app writes into the root, so such a directory holds nothing
    the ACL governs — and a leftover empty one (KICAD_DESKTOP_PROJECT pointing
    at a name that did not exist creates one, which is how `projects/none`
    appeared) would otherwise lock every account out of the desktop.

    What this route authorizes is *opening* the desktop, and that is a narrower
    claim than it may read as. The desktop is a long-lived session over a live
    bind mount, so a project created after someone opened it appears in their
    already-running session without another trip through here. Per-request
    membership cannot bound a session that never makes another request; closing
    that gap needs revocation inside the desktop, which does not exist. It is
    not a problem on a deployment with one operator, and it is the reason a
    deployment with accounts that are not on everything should name a single
    project in KICAD_DESKTOP_PROJECT instead of mounting the root.

    Every answer here is 204, 401 or 403, and nothing else — which is why this
    route resolves its own user instead of taking Depends(require_onboarded).
    nginx's auth_request understands exactly those denial codes and turns every
    other non-2xx into a bare 500 for the browser; `error_page 404` does not
    catch it, which is measurable:

        gate returns 403  -> client gets 302   (error_page runs)
        gate returns 404  -> client gets 500   "auth request unexpected status"
        gate returns 428  -> client gets 500   "auth request unexpected status"

    The dependency chain answers 428 for an unfinished profile, 404 for a
    non-member, 423 for a sealed project and 503 when Clerk is unconfigured, and
    every one of those reached the browser as a server error. Collapsing them to
    403 loses nothing: this response is read by nginx, never rendered, and the
    person is redirected to sign in either way.
    """
    try:
        user = require_user(request, session, authorization, __session)
        if not profile_mod.is_complete(user):
            raise HTTPException(status_code=403, detail="onboarding incomplete")

        project_id = (os.environ.get("KICAD_DESKTOP_PROJECT") or "").strip()
        if project_id and project_id != "none":
            # Raises exactly as every other project route does when the caller
            # is not a member.
            _project_dir(session, user, project_id)
        else:
            known = set(session.execute(select(Project.name)).scalars())
            if workspace_files.mount_absent():
                # The desktop mounts the projects this backend cannot see, so
                # what is mounted is unknowable here: assume every project is,
                # rather than reading the empty mask as "none" and letting
                # anyone in (Codex, PR #20).
                mounted = set(known)
            else:
                try:
                    mounted = {d.name for d in PROJECTS_ROOT.iterdir() if d.is_dir()}
                except FileNotFoundError:
                    mounted = set()
            mine = {p.name for p in projectacl.visible(session, user)}
            # Checked against `visible`, not `require_member` in a loop: one
            # query, and a sealed project does not become a 423 that would deny
            # the whole desktop over one project nobody has opened yet. Its
            # files are ciphertext on disk either way — useless in KiCad, but
            # not a leak.
            if not ((mounted & known) <= mine):
                raise HTTPException(status_code=403, detail="not a member of every mounted project")
    except HTTPException as exc:
        # 401 is the one code worth preserving: it is what nginx expects for
        # "not signed in", and it is already correct. Everything else becomes
        # 403 so that no denial can arrive at the browser as a 500.
        raise HTTPException(status_code=401 if exc.status_code == 401 else 403) from exc

    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health() -> dict:
    # Reported, never raised: `kicad_cli: null` is the answer when KiCad is
    # missing, and subprocess raises FileNotFoundError for a binary that is
    # not on PATH — which made the health check a 500 exactly when it had
    # something to say.
    try:
        kicad = subprocess.run(
            ["kicad-cli", "--version"], capture_output=True, text=True, check=False, timeout=10
        ).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        kicad = None
    return {
        "status": "ok",
        "projects_root": str(PROJECTS_ROOT),
        "kicad_cli": kicad,
    }


@app.get("/api/me")
def whoami(user: User = Depends(require_user)) -> dict:
    """Who the caller is, as this server understands them.

    The one authenticated route the UI can call to confirm a session actually
    works end to end. Clerk tells the browser it is signed in; this says the
    backend agrees — that the token verified against our issuer and resolved to
    a row here. Those can disagree (a misconfigured issuer, a token from another
    instance), and when they do the browser's own state is the misleading one.
    """
    return {"id": user.id, "clerk_user_id": user.clerk_user_id, "email": user.email}


class PassphraseBody(BaseModel):
    passphrase: str


class SetupBody(BaseModel):
    """Everything the setup screen collects, in one request.

    One call rather than three, because a half-finished account is the state
    worth designing out: a master key with no provider, or a provider whose key
    was sealed under a passphrase the user then failed to confirm. The whole
    thing commits or none of it does.
    """

    passphrase: str
    provider: str
    api_key: str = ""
    model: str = ""
    base_url: str = ""


@app.get("/api/providers")
def list_providers() -> list[dict]:
    """The provider catalog for the setup form. No secrets, nothing
    installation-specific, so it needs no session — the sign-in screen and the
    setup screen can both render before anything is known about the user."""
    return provider_catalog.as_json()


@app.get("/api/onboarding")
def onboarding_state(
    user: User = Depends(require_user), session: Session = Depends(session_scope)
) -> dict:
    """What the client needs to decide between setup, unlock, and the app."""
    return {
        "complete": profile_mod.is_complete(user),
        "has_passphrase": profile_mod.is_set_up(user),
        "endpoints_with_keys": sorted(keystore.endpoints_with_keys(session, user)),
    }


@app.post("/api/onboarding")
def complete_onboarding(
    body: SetupBody,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(session_scope),
) -> dict:
    """Set the encryption passphrase, register the first provider, and open the gate.

    Deliberately not behind require_onboarded — it is the one route that exists
    to *stop* being un-onboarded.
    """
    if profile_mod.is_complete(user):
        raise HTTPException(status_code=400, detail="this account is already set up")

    info = provider_catalog.get(body.provider)
    if info is None:
        raise HTTPException(status_code=400, detail=f"unknown provider {body.provider!r}")
    if info.needs_key and not body.api_key.strip():
        raise HTTPException(status_code=400, detail=f"{info.label} needs an API key")
    if info.needs_base_url and not body.base_url.strip():
        raise HTTPException(status_code=400, detail=f"{info.label} needs a base URL")

    try:
        master_key = profile_mod.set_passphrase(session, user, body.passphrase)
    except profile_mod.AlreadySetUp as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:  # too short
        raise HTTPException(status_code=400, detail=str(exc))

    # The endpoint is named for the provider it is. A second Anthropic account
    # later becomes a second endpoint with its own name and its own key, which
    # is what the registry is for.
    endpoint_name = body.provider
    _check_egress([egress.Target(endpoint_name, info.kind, body.base_url.strip() or info.base_url)])
    cfg = llmconfig.load(session, user)
    cfg.endpoints[endpoint_name] = appconfig.Endpoint(
        name=endpoint_name,
        kind=info.kind,
        model=body.model.strip() or info.default_model,
        base_url=body.base_url.strip() or info.base_url,
        auth="vault" if info.needs_key else "none",
        vision=info.vision,
    )
    # Route everything here for now. One provider is the whole point of the
    # setup screen; splitting tasks across several is a Settings decision made
    # once there is more than one to split between.
    cfg.tasks["default"] = [endpoint_name]
    llmconfig.save(session, user, cfg)

    if info.needs_key:
        keystore.put(session, master_key, user, endpoint_name, body.api_key)

    # The address other people seal project keys to. Made here rather than on
    # first use because "has finished setting up" and "can be shared with"
    # should be the same state — otherwise an owner is told their colleague has
    # not finished, when from that colleague's side everything looks done.
    grants.ensure_keypair(session, user, master_key)
    profile_mod.mark_complete(session, user)
    unlocked.put(
        unlock_mod.session_key_for(user.clerk_user_id, getattr(request.state, "clerk_claims", {})),
        master_key,
    )
    return {"ok": True, "endpoint": endpoint_name}


@app.post("/api/auth/unlock")
def auth_unlock(
    body: PassphraseBody,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(session_scope),
) -> dict:
    """Open this session with the encryption passphrase."""
    try:
        master_key = profile_mod.unlock(user, body.passphrase)
    except userkey.WrongPassphrase:
        raise HTTPException(status_code=401, detail="wrong passphrase")
    except profile_mod.NotSetUp as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _open_session(session, request, user, master_key)
    return {"unlocked": True}


def _open_session(session: Session, request: Request, user: User, master_key: bytes) -> None:
    """Everything that must happen when a session gains a master key.

    One function because there are now two doors into it — passphrase and
    passkey — and the backfills below are the kind of thing that gets added to
    whichever door the author was looking at. A passkey unlock that skipped them
    would leave an account without a keypair, which surfaces much later as a
    colleague being told they cannot be shared with.
    """
    # Accounts that onboarded before keypairs existed acquire one the next time
    # they unlock, rather than needing a migration that could not have run —
    # sealing a private key needs a master key, which a migration never has.
    grants.ensure_keypair(session, user, master_key)
    # Projects that predate per-project keys get one here, for the same reason
    # the keypair does: this is the first moment a master key exists.
    touched = grants.backfill_for_owner(session, user, master_key)
    if touched:
        logger.info("minted or completed project keys for %d project(s)", touched)
    unlocked.put(
        unlock_mod.session_key_for(user.clerk_user_id, getattr(request.state, "clerk_claims", {})),
        master_key,
    )


# --- Passkeys -----------------------------------------------------------------
#
# The ceremony is two round trips by design: the server issues a challenge, the
# authenticator signs it, the server checks the one it issued. Anything shorter
# is replayable.
#
# Challenges live in this process (see passkeys.Challenges) keyed by Clerk
# session, so two browsers can be part-way through their own ceremonies without
# colliding.

challenges = passkeys.Challenges()


def _ceremony_key(request: Request, user: User) -> str:
    return unlock_mod.session_key_for(
        user.clerk_user_id, getattr(request.state, "clerk_claims", {})
    )


def _relying_party(request: Request) -> passkeys.RelyingParty:
    try:
        return passkeys.relying_party(request.headers.get("origin", ""))
    except passkeys.PasskeyError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/passkeys")
def list_passkeys(
    user: User = Depends(require_user), session: Session = Depends(session_scope)
) -> list[dict]:
    return [
        {
            "id": c.id,
            "label": c.label or "Passkey",
            "created_at": _iso(c.created_at),
        }
        for c in passkeys.list_for(session, user)
    ]


class PasskeyRegisterBegin(BaseModel):
    label: str = ""


@app.post("/api/passkeys/register/begin")
def passkey_register_begin(
    request: Request,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Options for creating a passkey.

    Requires an unlocked session, and that is the whole security of enrolment:
    the new credential wraps the master key, so whoever adds one must already be
    able to produce it. Signing in with Clerk alone cannot enrol a key.
    """
    rp = _relying_party(request)
    options, challenge = passkeys.registration_options(
        user, passkeys.list_for(session, user), rp
    )
    salt = base64.urlsafe_b64decode(options["extensions"]["prf"]["eval"]["first"] + "==")
    challenges.issue(_ceremony_key(request, user), challenge, salt)
    return options


class PasskeyRegisterFinish(BaseModel):
    credential: dict
    # Base64url of the authenticator's PRF output. See app/passkeys.py for why
    # this crosses the wire at all: the same reason the passphrase does.
    prf_output: str
    label: str = ""


@app.post("/api/passkeys/register/finish")
def passkey_register_finish(
    body: PasskeyRegisterFinish,
    request: Request,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    rp = _relying_party(request)
    try:
        pending = challenges.take(_ceremony_key(request, user))
        row = passkeys.register(
            session,
            user,
            body.credential,
            _b64url(body.prf_output),
            master_key,
            pending.challenge,
            pending.salt or b"",
            rp,
            body.label,
        )
    except passkeys.PasskeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "id": row.id, "label": row.label or "Passkey"}


@app.post("/api/passkeys/auth/begin")
def passkey_auth_begin(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(session_scope),
) -> dict:
    """Options for unlocking with a passkey.

    Deliberately behind Clerk like every other route: this is the second factor,
    not the first, and it answers "which credentials does this account have" —
    which is not a question an anonymous caller gets to ask.
    """
    rp = _relying_party(request)
    try:
        options, challenge = passkeys.authentication_options(
            passkeys.list_for(session, user), rp
        )
    except passkeys.NoPasskeys as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    challenges.issue(_ceremony_key(request, user), challenge)
    return options


class PasskeyAuthFinish(BaseModel):
    credential: dict
    prf_output: str


@app.post("/api/passkeys/auth/finish")
def passkey_auth_finish(
    body: PasskeyAuthFinish,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(session_scope),
) -> dict:
    try:
        pending = challenges.take(_ceremony_key(request, user))
        master_key = passkeys.authenticate(
            session, user, body.credential, _b64url(body.prf_output), pending.challenge, rp=_relying_party(request)
        )
    except passkeys.PasskeyError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except VaultError as exc:
        # The unwrap failed: a verified assertion whose PRF output does not open
        # the blob. Re-registering the key is the fix, so say that rather than
        # "wrong passkey".
        raise HTTPException(
            status_code=401,
            detail=f"{exc} — remove this passkey and add it again",
        )
    _open_session(session, request, user, master_key)
    return {"unlocked": True}


@app.delete("/api/passkeys/{credential_row_id}")
def delete_passkey(
    credential_row_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(session_scope),
) -> dict:
    """Removing the last passkey is allowed — the passphrase slot always exists,
    so nobody can delete their way out of their own data."""
    if not passkeys.forget(session, user, credential_row_id):
        raise HTTPException(status_code=404, detail="no such passkey")
    return {"ok": True}


def _b64url(value: str) -> bytes:
    """Decode base64url from the browser, which omits padding."""
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"malformed base64url: {exc}")


@app.post("/api/auth/lock")
def auth_lock(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(session_scope),
) -> dict:
    """Forget this session's key without signing out of Clerk. Two separate
    things: you can be signed in and locked, which is the state after a server
    restart."""
    unlocked.drop(
        unlock_mod.session_key_for(user.clerk_user_id, getattr(request.state, "clerk_claims", {}))
    )
    # Locking says "I am done", so the workspaces this user could reach go back
    # to sealed. Only the ones nobody else is in: a project open because a
    # colleague is working on it is not yours to close.
    sealed = []
    for project in projectacl.visible(session, user):
        # release() is false while a colleague is still in there — one project is
        # one directory tree shared by its members, so sealing on your lock alone
        # would delete the files out from under them mid-edit.
        openstate.release(session, project.name, user.id)
        if workspaces.release(project.name, user.id) and _seal_workspace(
                session, project.name, reason=f"{user.email} locked their session"):
            sealed.append(project.name)
    return {"unlocked": False, "sealed": sealed}


@app.get("/api/auth/lock-state")
def lock_state(request: Request, user: User = Depends(require_user)) -> dict:
    claims = getattr(request.state, "clerk_claims", {}) or {}
    key = unlocked.get(unlock_mod.session_key_for(user.clerk_user_id, claims))
    return {"unlocked": key is not None, "has_passphrase": profile_mod.is_set_up(user)}


@app.get("/api/auth/config")
def auth_config() -> dict:
    """Unauthenticated: whether this server can accept sign-ins at all.

    Lets the UI distinguish "you are signed out" from "this deployment has no
    Clerk issuer set", which look identical from the browser and have completely
    different remedies.
    """
    return {"clerk_configured": clerk_auth.configured()}


# --------------------------------------------------------------------------
# Settings: LLM routing (server config) + this user's own API keys
# --------------------------------------------------------------------------


@app.get("/api/settings")
def get_settings(
    user: User = Depends(require_onboarded), session: Session = Depends(session_scope)
) -> dict:
    cfg = llmconfig.load(session, user)
    with_keys = keystore.endpoints_with_keys(session, user)
    return {
        # The registry: what exists, and which endpoints serve which job.
        #
        # has_key answers "will a run on this endpoint start", and it is about
        # THIS user: another user having a key for the same endpoint tells you
        # nothing about whether yours will run.
        "endpoints": [
            {
                "name": ep.name,
                "kind": ep.kind,
                "model": ep.resolved_model(),
                "base_url": ep.base_url,
                "auth": ep.auth,
                "vision": ep.can_see,
                # What was *declared*, not what was worked out — the settings
                # screen edits the override, and echoing an inferred figure back
                # into it would turn a guess into a stated fact on the next save.
                "context_tokens": ep.context_tokens,
                "max_output_tokens": ep.max_output_tokens,
                "needs_key": ep.needs_key,
                "has_key": not ep.needs_key or ep.name in with_keys,
            }
            for ep in cfg.endpoints.values()
        ],
        # Two maps, because they answer different questions and conflating them
        # deadlocked the settings screen.
        #
        # `tasks` is what is actually *stored*: a task absent here inherits the
        # default chain and has no route of its own. `effective` is what a
        # request would resolve to today, fallbacks applied, which is what the
        # screen should show.
        #
        # Returning only the effective map meant the client echoed inherited
        # values back as explicit routes on the next save. That turned a
        # perfectly legal "vision is unset" into an illegal
        # "vision routes to a blind endpoint", which validation then
        # refused — including refusing the very edit that would have fixed it.
        # A GET whose result cannot be PUT back unchanged is the bug.
        "tasks": {t: list(chain) for t, chain in cfg.tasks.items() if chain},
        "effective": {t: cfg.chain_for(t) for t in appconfig.KNOWN_TASKS},
        # What each endpoint's chosen model can actually do. Surfaced here, not
        # only in the endpoint editor, because task routing is where the
        # difference bites: review_panel wants several genuinely different
        # models, chat is better with one that reasons, and vision
        # simply cannot be served by a model that does not see. None of that is
        # guessable from an endpoint's name.
        "endpoint_capabilities": _endpoint_capabilities(cfg),
        # One source for these. A vision task routed at a blind endpoint and one
        # that merely inherits a blind default are the same problem, and were
        # reported by two separate pieces of code that disagreed about whether
        # it was fatal — which is what made the routing screen unsaveable.
        "warnings": cfg.warnings(),
        "known_kinds": list(appconfig.KNOWN_KINDS),
        "known_tasks": list(appconfig.KNOWN_TASKS),
        "vision_tasks": sorted(appconfig.VISION_TASKS),
        # Presence and timestamps only, and only this user's. The values never
        # leave the database except into a subprocess environment.
        "secrets": [
            {"provider": m.endpoint, "updated_at": m.updated_at}
            for m in keystore.list_meta(session, user)
        ],
        # Shows Settings -> Admin; every admin route checks for itself.
        "is_admin": egress.is_admin(user),
    }


class ProbeBody(BaseModel):
    """What to ask, and optionally the key to ask with.

    ``api_key`` exists for the sequence that otherwise cannot be completed: a
    hosted provider will not list its catalogue without a key, and until this
    existed a key could only be attached to an endpoint that had already been
    saved — which meant saving a guessed model name first, which is the thing
    the listing is for.

    The key is used for this one request and never stored. It travels in a body
    rather than a query string because query strings end up in access logs,
    proxy logs and browser history, and none of those are places for a
    credential.
    """

    kind: str
    base_url: str = ""
    name: str = ""
    api_key: str = ""


@app.post("/api/settings/llm/models")
def probe_models(
    body: ProbeBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Ask a provider which models it will actually answer to.

    Every kind here publishes a list, and every kind here has names that are not
    guessable. Ollama serves everything it holds on one port, so the port
    identifies the *server* and the name identifies the *model* —
    `hf.co/21world/KiCAD-MCP-Qwen3.5-4B-GGUF:latest` is not a string anyone
    should type from memory, and a typo becomes a run that fails at request
    time rather than at save time. The hosted providers have the opposite
    problem: their catalogues change under you, so a name that was right last
    quarter quietly stops being right.

    Asked by kind and URL rather than by saved endpoint, because the moment this
    is most needed is while *adding* one, when there is nothing saved to look up.

    A provider that cannot be reached is reported as such rather than as an
    empty list. A server that is down still has models; it just is not saying
    which, and those two answers must not look the same in a dropdown.
    """
    kind, base_url, name = body.kind, body.base_url, body.name
    if name:
        saved = llmconfig.load(session, user).endpoints.get(name)
        if saved is not None:
            kind = kind or saved.kind
            base_url = base_url or saved.base_url

    # A key typed into the form wins over a stored one, so a key can be checked
    # before it is committed — and a replacement can be verified before it
    # replaces one that currently works.
    key = body.api_key or (
        keystore.get(session, master_key, user, name) if name else None
    )
    headers: dict[str, str] = {}
    field = "data"

    if kind == "ollama":
        base = base_url or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
        url = _normalise_base(base).rstrip("/") + "/api/tags"
        field = "models"
    elif kind == "openai-compatible":
        if not base_url:
            raise HTTPException(status_code=400, detail="openai-compatible needs a base_url")
        url = _normalise_base(base_url).rstrip("/") + "/models"
        if key:
            headers["Authorization"] = f"Bearer {key}"
    elif kind == "openai":
        if not key:
            raise HTTPException(
                status_code=400,
                detail="OpenAI will not list models without a key — paste one above and try again",
            )
        url = (_normalise_base(base_url).rstrip("/") if base_url else "https://api.openai.com/v1") + "/models"
        headers["Authorization"] = f"Bearer {key}"
    elif kind == "anthropic":
        if not key:
            raise HTTPException(
                status_code=400,
                detail="Anthropic will not list models without a key — paste one above and try again",
            )
        url = (_normalise_base(base_url).rstrip("/") if base_url else "https://api.anthropic.com/v1") + "/models"
        headers["x-api-key"] = key
        # Required on every Anthropic request; without it the API refuses
        # rather than defaulting to anything.
        headers["anthropic-version"] = "2023-06-01"
    else:
        return {"kind": kind, "models": [], "asked": False,
                "detail": f"no model listing is defined for kind {kind!r}"}

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read())
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"could not reach {url} ({type(exc).__name__}: {exc})",
        )

    rows = payload.get(field) or []
    # `id` first, and the order is the whole bug this fixes.
    #
    # It used to read `model` → `name` → `id`, which is right for Ollama (whose
    # `name` *is* the wire identifier) and quietly wrong for anything that
    # publishes both. OpenRouter's catalogue gives id `google/gemini-3.7-flash`
    # and name `Google: Gemini 3.7 Flash`; the display string won, went into the
    # dropdown, and got saved as the model — so an endpoint picked from a list
    # this app generated could never resolve on the wire.
    #
    # Ollama is unaffected: it has no `id`, and its `model` and `name` are the
    # same string.
    pairs: dict[str, str] = {}
    for m in rows:
        if not isinstance(m, dict):
            continue
        wire = str(m.get("id") or m.get("model") or m.get("name") or "").strip()
        if not wire:
            continue
        # A human label where the provider offers one, so a list of four hundred
        # router models is readable — but it never becomes the stored value.
        label = str(m.get("name") or m.get("display_name") or "").strip()
        pairs.setdefault(wire, label if label and label != wire else "")
    names = sorted(pairs)
    return {
        "kind": kind,
        "asked": True,
        "url": url,
        "models": names,
        # id → human label, for a picker that shows one and stores the other.
        "labels": {k: v for k, v in pairs.items() if v},
        # Which of them can actually read an image. Asked rather than assumed:
        # `vision = true` on an endpoint whose model is blind produces a
        # vision route that validates fine and then fails at request
        # time, which is the least useful place to discover it.
        "capabilities": _model_capabilities(kind, url, names, headers),
    }


def _endpoint_capabilities(cfg) -> dict[str, list[str]]:
    """Capabilities per configured endpoint, for the kinds that will say.

    Cheap enough to do on a settings load: one local HTTP call per endpoint,
    against a server on the same host or LAN. Hosted providers are skipped —
    probing them is a billable call and their catalogues are documented.

    An endpoint missing from the result is *unknown*, not incapable.
    """
    out: dict[str, list[str]] = {}
    for name, ep in cfg.endpoints.items():
        if ep.kind not in ("ollama", "openai-compatible"):
            continue
        model = ep.resolved_model()
        if not model:
            continue
        try:
            if ep.kind == "ollama":
                base = ep.base_url or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
                url = _normalise_base(base).rstrip("/") + "/api/tags"
            else:
                if not ep.base_url:
                    continue
                url = _normalise_base(ep.base_url).rstrip("/") + "/models"
            caps = _model_capabilities(ep.kind, url, [model], {})
            if caps.get(model):
                out[name] = caps[model]
        except Exception:
            continue
    return out


def _model_capabilities(
    kind: str, url: str, names: list[str], headers: dict
) -> dict[str, list[str]]:
    """What each model can do, asked of the server rather than inferred.

    Ollama publishes this directly: ``/api/show`` returns a capability list —
    ``vision``, ``tools``, ``thinking``, ``completion``, ``embedding`` — which
    is free, authoritative, and explains behaviour that is otherwise puzzling.
    A model tagged ``thinking`` emits chain-of-thought that a caller has to
    parse out; one without ``tools`` will not call a tool no matter how the
    prompt is written.

    An OpenAI-compatible server does not publish capabilities, so vision is
    established the only definitive way: send a one-pixel image and see whether
    it refuses. vLLM answers "is not a multimodal model" with a 400, which
    beats guessing from the model's name.

    A model absent from the result is *unknown*, not incapable — the same
    distinction the rest of this codebase keeps. Hosted providers are left
    unknown deliberately: their catalogues are large, probing is a billable
    call per model, and their capabilities are documented.
    """
    caps: dict[str, list[str]] = {}

    if kind == "ollama":
        base = url[: -len("/api/tags")]
        for n in names:
            try:
                req = urllib.request.Request(
                    base + "/api/show",
                    data=json.dumps({"model": n}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    got = json.loads(r.read()).get("capabilities") or []
                if got:
                    caps[n] = sorted(str(c) for c in got)
            except Exception:
                continue
        return caps

    if kind == "openai-compatible" and len(names) <= 4:
        # Bounded: a server hosting one or two models is worth probing; a
        # gateway fronting two hundred is not.
        base = url[: -len("/models")]
        for n in names:
            body = {
                "model": n, "max_tokens": 1,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "."},
                    {"type": "image_url", "image_url": {"url": _ONE_PIXEL_PNG}},
                ]}],
            }
            try:
                req = urllib.request.Request(
                    base + "/chat/completions", data=json.dumps(body).encode(),
                    headers={**headers, "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=20):
                    caps[n] = ["completion", "vision"]
            except urllib.error.HTTPError:
                caps[n] = ["completion"]     # answered, and refused the image
            except Exception:
                continue                      # no answer — unknown, not blind
        return caps

    return caps


# A 1x1 red PNG. Small enough that probing costs nothing and any server that
# can decode an image at all will accept it.
_ONE_PIXEL_PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAA"
    "DUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _normalise_base(base: str) -> str:
    """Accept what an operator actually types.

    `ollama:11434`, `blpl-ollama` and `172.18.0.8:11434` are all reasonable
    things to write in a box labelled "base URL", and every one of them fails as
    a URL because it has no scheme — urllib reads the host as a relative path
    and the request goes nowhere, with an error naming neither problem. A
    missing scheme is not ambiguous here, so it is filled in rather than
    refused.
    """
    base = base.strip()
    if base and "://" not in base:
        base = "http://" + base
    return base


class EndpointBody(BaseModel):
    name: str
    kind: str
    model: str = ""
    base_url: str = ""
    auth: str = "vault"
    vision: bool | None = None
    # Null means "work it out". Stated when neither discovery nor the model
    # name gets it right — which is the ordinary case for a self-hosted model.
    context_tokens: int | None = None
    max_output_tokens: int | None = None


class LlmSettingsBody(BaseModel):
    """The registry: which endpoints exist, and which serve which task.

    The older ``priority``/``models`` shape is gone. It existed so a settings
    screen that thought in providers kept working; that screen now speaks in
    named endpoints, and keeping a second way to say the same thing meant two
    code paths that could disagree about what was configured.
    """

    endpoints: list[EndpointBody] | None = None
    tasks: dict[str, list[str]] | None = None


@app.put("/api/settings/llm")
def put_llm_settings(
    body: LlmSettingsBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    cfg = llmconfig.load(session, user)

    if body.endpoints is not None:
        _check_egress([egress.Target(e.name, e.kind, e.base_url or "") for e in body.endpoints])
        cfg.endpoints = {
            e.name: appconfig.Endpoint(
                name=e.name,
                kind=e.kind,
                model=e.model,
                base_url=e.base_url,
                auth=e.auth,
                vision=e.vision,
                context_tokens=e.context_tokens,
                max_output_tokens=e.max_output_tokens,
            )
            for e in body.endpoints
        }
    if body.tasks is not None:
        # Merge. An empty chain means "unset this task, let it inherit again",
        # which is the only way back for a task that should never have had a
        # route of its own.
        merged = dict(cfg.tasks)
        for t, chain in body.tasks.items():
            if chain:
                merged[t] = list(chain)
            else:
                merged.pop(t, None)
        cfg.tasks = merged

    try:
        llmconfig.save(session, user, cfg)
    except ValueError as exc:  # unknown kind or endpoint, empty default chain
        raise HTTPException(status_code=400, detail=str(exc))
    # Saved, and here is what is odd about it. Warnings ride back on the success
    # response rather than becoming a refusal: a configuration that will run in
    # a way you may not have meant is worth saying, and is not worth making the
    # screen unable to save.
    return {"ok": True, "warnings": cfg.warnings()}


class SecretBody(BaseModel):
    value: str


def _check_egress(targets: list) -> None:
    """Refuse an LLM endpoint a run in a workspace could not reach (egress.py)."""
    try:
        egress.check(targets)
    except egress.EgressRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def require_admin(user: User = Depends(require_onboarded)) -> User:
    """An administrator: BLPL_ADMIN_EMAILS names their verified email.

    404 for anyone else, as for a route that does not exist: the admin
    surface is not something an ordinary account needs to learn about."""
    if not egress.is_admin(user):
        raise HTTPException(status_code=404, detail="Not Found")
    return user


@app.get("/api/admin/egress")
def get_admin_egress(user: User = Depends(require_admin),
                     session: Session = Depends(session_scope)) -> dict:
    """Network access for runs in workspaces: the shipped defaults (read
    only), the server-wide hosts administrators added, and who did what."""
    from .models import EgressHost, EgressHostEvent

    by_id = {u.id: u.email for u in session.scalars(select(User))}
    hosts = session.scalars(select(EgressHost).order_by(EgressHost.host)).all()
    events = session.scalars(
        select(EgressHostEvent).order_by(EgressHostEvent.at.desc(),
                                     EgressHostEvent.id.desc()).limit(50)).all()
    return {
        "enforced": egress.enforced(),
        "defaults": egress.defaults(),
        "git_ssh_hosts": egress.git_ssh_hosts(),
        "hosts": [{"host": h.host, "ssh": bool(h.ssh), "added_by": by_id.get(h.added_by, ""),
                   "added_at": h.added_at} for h in hosts],
        "events": [{"host": e.host, "action": e.action, "ssh": bool(e.ssh), "by": e.email,
                    "at": e.at} for e in events],
    }


class EgressHostBody(BaseModel):
    host: str
    # Also git over ssh (port 22). Explicit per entry, never implied by HTTPS.
    # Posting an existing host with a different value changes just this.
    ssh: bool = False


@app.post("/api/admin/egress/hosts")
def add_admin_egress_host(body: EgressHostBody, user: User = Depends(require_admin),
                          session: Session = Depends(session_scope)) -> dict:
    """Allow a host for every run, from the next new connection: Squid asks
    the control plane per host, so nothing is reloaded."""
    try:
        host = egress.add_host(session, user, body.host, ssh=body.ssh)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("egress: %s set %s (ssh %s)", user.email, host, "on" if body.ssh else "off")
    return {"ok": True, "host": host, "ssh": body.ssh}


@app.delete("/api/admin/egress/hosts/{host}")
def remove_admin_egress_host(host: str, user: User = Depends(require_admin),
                             session: Session = Depends(session_scope)) -> dict:
    if not egress.remove_host(session, user, host):
        raise HTTPException(status_code=404, detail=f"{host} is not on the server-wide list")
    logger.info("egress: %s removed %s", user.email, host)
    return {"ok": True}


@app.put("/api/settings/secrets/{provider}")
def put_secret(
    provider: str,
    body: SecretBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Store one of *this user's* keys. The path parameter is an endpoint name."""
    try:
        keystore.put(session, master_key, user, provider, body.value)
    except (ValueError, keystore.KeystoreError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "provider": provider}


@app.delete("/api/settings/secrets/{provider}")
def delete_secret(
    provider: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    return {"ok": True, "removed": keystore.delete(session, user, provider)}


@app.get("/api/library-policy")
def library_policy_defaults(user: User = Depends(require_onboarded)) -> dict:
    """The choices and the consent wording, for a dialog with no project yet.

    The New Project form asks for the declaration before the project exists, so
    it cannot read /api/projects/{id}/policy. Same source of truth either way —
    this returns the module's constants, not a copy."""
    from . import library_policy

    return {
        "defaults": dict(library_policy.DEFAULTS),
        "choices": {
            "contribute": list(library_policy.CONTRIBUTE),
            "consume": list(library_policy.CONSUME),
            "unique_components": list(library_policy.UNIQUE),
        },
        "consent_text": library_policy.CONSENT_TEXT,
    }


class GitEndpointBody(BaseModel):
    host: str
    method: str
    secret: str
    username: str | None = None


@app.get("/api/settings/git")
def list_git_endpoints(
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """This user's git credentials — names and hosts, never secrets — plus the
    presets the form pre-fills. Presets are advisory: by the time a row is
    stored, GitHub is just a host like any other."""
    from . import gitstore

    return {"endpoints": gitstore.list_endpoints(session, user), "presets": gitstore.PRESETS,
            "methods": list(gitstore.METHODS)}


@app.put("/api/settings/git/{name}")
def put_git_endpoint(
    name: str,
    body: GitEndpointBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Store one git credential. The secret is a PAT for HTTPS or a private key
    for SSH, sealed under this user's master key exactly as provider keys are —
    it never appears in a response body."""
    from . import gitstore

    try:
        gitstore.put(
            session, master_key, user,
            name=name, host=body.host, method=body.method,
            secret=body.secret, username=body.username,
        )
    except gitstore.GitStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "name": name.strip().lower()}


@app.delete("/api/settings/git/{name}")
def delete_git_endpoint(
    name: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    from . import gitstore

    return {"ok": True, "removed": gitstore.delete(session, user, name)}


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------


def _refuse_taken_name(session: Session, name: str) -> None:
    """Refuse a name someone else already has.

    Names are globally unique because they are directory names on disk, so a
    clash is not "you already have one" — it may be a project you cannot see.
    The message says only that the name is taken, because saying who has it
    would leak exactly what the 404-not-403 rule exists to hide.
    """
    if session.scalar(select(Project).where(Project.name == name)) is not None:
        raise HTTPException(status_code=409, detail=f"the name {name!r} is already taken")


def _project_or_404(session: Session, user: User, project_id: str) -> Project:
    """The project row, or a 404 — never a raw NoSuchProject.

    projectacl raises a plain LookupError, which is right for a library and
    wrong at a route boundary: it escapes as a 500, so a non-member gets "the
    server broke" instead of "there is no such project", and the 404-not-403
    rule quietly stops holding. One helper rather than a try/except at each of
    five call sites, because the sixth is the one that gets forgotten.
    """
    try:
        return projectacl.require_member(session, user, project_id)
    except projectacl.NoSuchProject:
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")


def _project_dir(session: Session, user: User, project_id: str, *,
                 agent_ready: bool = False) -> Path:
    """The working copy, if this user may open it.

    The one place project permission is enforced, which is why every route
    reaches the filesystem through here rather than through projects.project_dir
    directly. A check that some routes perform and others skip is worse than
    none: it reads as enforced.

    A non-member gets the same 404 as a project that does not exist. 403 would
    confirm it does, which is enough to enumerate other people's project names
    one guess at a time.
    """
    project = _project_or_404(session, user, project_id)
    if workspace_files.mount_absent() and not agent_ready:
        # This deployment runs the backend without the project mount, and
        # this route still reads project files through it. Said plainly,
        # rather than reading an empty directory and answering as though the
        # project had no files.
        raise HTTPException(status_code=503, detail=(
            "this view still reads project files directly, and this deployment runs "
            "without the project mount (BLPL_PROJECT_MOUNT=absent)"))
    if workspace.is_sealed(PROJECTS_ROOT, project_id):
        # Encrypted at rest and nobody has opened it. Not an error and not a
        # permission problem — 423 so the client offers to open it rather than
        # reporting a project that appears to have vanished.
        #
        # The header is what separates this from the other 423 on these routes,
        # which means "your session has no key". They want opposite responses —
        # decrypt and retry versus send the user to unlock — and telling them
        # apart by matching on the prose of a message would break the first time
        # somebody reworded it.
        raise HTTPException(
            status_code=423,
            detail=f"{project_id!r} is sealed; open it to decrypt it",
            headers={"X-BLPL-Sealed": project_id},
        )
    _touch_workspace(session, project_id, user.id)
    if workspace_files.enabled():
        try:
            projects.project_dir(project_id)  # the name's guard, lexical
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        try:
            return workspace_files.ensure_worktree(
                project_id, user.id, project.owner_id == user.id)
        except ProjectError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    try:
        # Validates the name before it reaches the filesystem — the guard against
        # ../ lives there, and a worktree path is built from the same name.
        projects.project_dir(project_id)
        d = worktrees.ensure(
            PROJECTS_ROOT, project_id, user.id, is_owner=project.owner_id == user.id
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not d.is_dir():
        # Registered but not on disk: a real inconsistency rather than a
        # permission problem, and worth saying so instead of pretending it is
        # missing.
        raise HTTPException(
            status_code=409, detail=f"project {project_id!r} is registered but has no working copy"
        )
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


def _latest_with_origin(
    pipeline_dir: Path, suffix: str, stem_contains: str | None = None
) -> tuple[Path | None, bool]:
    """``(path, is_archived)`` — is_archived is True when only a rotated copy exists.

    ``stem_contains`` narrows to one board. Without it a multi-board project
    returns whichever emitted file is newest, which is a different board than
    the one on screen with nothing on the page saying so.
    """
    if not pipeline_dir.is_dir():
        return None, False

    def match(paths: list[Path]) -> list[Path]:
        if stem_contains is None:
            return paths
        return [p for p in paths if stem_contains in p.name]

    live = match(sorted(pipeline_dir.glob(f"*{suffix}"), reverse=True))
    if live:
        return live[0], False
    # rglob so any rotation layout is caught, not just archive/ specifically.
    archived = match(sorted(pipeline_dir.rglob(f"*{suffix}"), reverse=True))
    return (archived[0], True) if archived else (None, False)


def _fab_readiness(pipeline_dir: Path) -> dict | None:
    """A one-glance verdict from the latest Stage 8 review, or None if not run.

    Surfaces the state that must never be missed — placeholder parts and emitter
    defects — at the project level, so 'this board is not fabricable' is visible
    without opening it. Best-effort: a missing or unparseable report is just None.
    """
    # A single-board project writes review_report.json; a multi-board project
    # writes one per board (review_report.<board>.json). The verdict shown at
    # the project level is the worst of them — one unfabricable board makes
    # the project unfabricable — rather than silently None because the
    # unqualified name does not exist.
    candidates = [pipeline_dir / "review_report.json"] if (pipeline_dir / "review_report.json").is_file() \
        else sorted(pipeline_dir.glob("review_report.*.json"))
    if not candidates:
        return None
    data: dict = {"summary": {"placeholders": 0, "emitter": 0}}
    seen = False
    for report in candidates:
        try:
            one = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        seen = True
        s1 = one.get("summary") or {}
        data["summary"]["placeholders"] += int(s1.get("placeholders", 0))
        data["summary"]["emitter"] += int(s1.get("emitter", 0))
    if not seen:
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
def list_projects(
    user: User = Depends(require_onboarded), session: Session = Depends(session_scope)
) -> list[dict]:
    """The projects this user owns or has been shared into.

    Driven by membership, not by scanning PROJECTS_ROOT. The directory listing
    is what showed every user every project — the filesystem knows what exists,
    not who it belongs to, and it never will.
    """
    out = []
    for project in projectacl.visible(session, user):
        d = PROJECTS_ROOT / project.name
        sealed = workspace.is_sealed(PROJECTS_ROOT, project.name)
        # Without the project mount nothing here can be read, so an unsealed
        # project is listed with its details unknown rather than dropped.
        unseen = not sealed and workspace_files.mount_absent()
        if not d.is_dir() and not sealed and not unseen:
            continue  # registered but no working copy; not this endpoint's problem
        unknown = sealed or unseen
        pipeline = d / ".pipeline"
        out.append(
            {
                "id": project.name,
                # A sealed project is encrypted, not gone, and it must keep its
                # place in the list — skipping it made "seal" look like "delete"
                # to anyone watching the dashboard. What is unknown while sealed
                # is reported as unknown rather than as absent: reading these
                # would mean decrypting, which is exactly what has not happened.
                "sealed": sealed,
                "markdown_files": None if unknown else len(list(d.glob("*.md"))),
                "has_schematic": None if unknown else _latest(pipeline, ".kicad_sch") is not None,
                "has_pcb": None if unknown else _latest(pipeline, ".kicad_pcb") is not None,
                "is_git": None if unknown else (d / ".git").is_dir(),
                "fab": None if unknown else _fab_readiness(pipeline),
                "owned": project.owner_id == user.id,
                "shared_with": len(project.members) - 1,
            }
        )
    return out


class PolicyFields(BaseModel):
    """A project's declaration about the shared component library.

    Optional on the wire and closed when omitted, which is the only safe way for
    a field to be missing: an older client, a truncated body or a forgotten
    parameter must never be the reason a project starts sharing. The dialog asks
    for all three explicitly — "we never asked, so we assumed the permissive
    thing" is the failure this exists to prevent — but the server does not rely
    on the dialog to be the thing that keeps it closed.
    """

    contribute: str = "never"
    consume: str = "ask"
    unique_components: str = "never_contribute"
    #: True only when the person ticked the consent wording currently in force.
    consented: bool = False


class CloneBody(PolicyFields):
    """Clone an existing repository into a new project.

    Asks what creation asks beyond the remote itself, for the same reasons: a
    push policy chosen on purpose (no default — the manual answer is a risk to
    read, not to inherit), and the cloner's commit identity for the project,
    with the option of making it their default. Checked with sentences rather
    than left to pydantic's 422, as InitBody is.
    """

    name: str
    remote: str
    branch: str = "main"
    push_policy: str | None = None
    git_name: str = ""
    git_email: str = ""
    save_as_default: bool = False


@app.get("/api/dashboard")
def dashboard(
    user: User = Depends(require_onboarded), session: Session = Depends(session_scope)
) -> dict:
    """Everything the landing screen needs, in one request.

    One call rather than three, because the dashboard is the first thing after
    unlock and three round trips is three chances to render half a page. The
    sort keys come down with the projects so the client can reorder without
    asking again.
    """
    projects_ = projectacl.visible(session, user)
    mine = activity.last_touched_by(session, user)
    theirs = activity.last_activity(session, [p.id for p in projects_])

    out = []
    for project in projects_:
        d = PROJECTS_ROOT / project.name
        pipeline = d / ".pipeline"
        exists = d.is_dir()
        out.append(
            {
                "id": project.name,
                "owned": project.owner_id == user.id,
                "members": len(project.members),
                # Sealed reads as an ordinary state here — a project waiting to
                # be opened, with a lock on the card rather than a row of zeroes
                # that would suggest an empty project.
                "sealed": workspace.is_sealed(PROJECTS_ROOT, project.name),
                "markdown_files": len(list(d.glob("*.md"))) if exists else 0,
                "has_schematic": exists and _latest(pipeline, ".kicad_sch") is not None,
                "has_pcb": exists and _latest(pipeline, ".kicad_pcb") is not None,
                "is_git": exists and (d / ".git").is_dir(),
                "fab": _fab_readiness(pipeline) if exists else None,
                # Two different questions, so two different keys. "When did I
                # last touch this" is where you left off; "when did anything
                # happen" is what moved while you were away, which is the useful
                # one for a project somebody shared with you.
                "last_touched_by_me": _iso(mine.get(project.id)),
                "last_activity": _iso(theirs.get(project.id)),
            }
        )
    return {
        "projects": out,
        "activity": [activity.as_json(a) for a in activity.feed_for(session, user)],
        "invitations": [
            {
                "id": inv.id,
                "project": inv.project.name,
                "invited_by": inv.invited_by.email,
                "expires_at": inv.expires_at.isoformat(),
            }
            for inv in projectacl.pending_for(session, user)
        ],
    }


def _iso(when) -> str | None:
    return when.isoformat() if when is not None else None


@contextmanager
def _git_credentials(session: Session, master_key: bytes, user: User, remote: str):
    """The environment for one remote git operation: a leased credential, or None.

    None is the public-repository case and must stay working — it was the only
    case that ever worked. A row that matches the remote's host is decrypted for
    the length of the operation and gone after; see gitstore.lease for why HTTPS
    rides the environment and SSH is briefly a file.
    """
    from . import gitstore

    try:
        row = gitstore.for_remote(session, user, remote)
    except gitstore.GitStoreError as exc:
        # Two legacy rows for one host: refused rather than guessed. Raised as
        # the ProjectError clone, pull and push already turn into a 400, so the
        # user reads which credentials collide instead of a 500.
        raise ProjectError(str(exc)) from exc
    if row is None:
        yield None
        return
    with gitstore.lease(master_key, row) as cred:
        yield cred.env


@app.post("/api/projects/clone")
def clone_project(
    body: CloneBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Clone a remote into a new working copy, owned by whoever cloned it."""
    _refuse_taken_name(session, body.name)
    try:
        # Validated before any credential is looked up for it: the lookup parses
        # the URL itself, and a credential should never be chosen for a remote
        # that is about to be refused.
        check_remote_url(body.remote)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Then the choices, all before git runs: a clone that succeeded and was
    # then refused for a missing push policy would leave a working copy no
    # project row points at.
    try:
        policy = autopush.validate_policy(body.push_policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        identity = gitidentity.validate(body.git_name, body.git_email)
    except gitidentity.IdentityError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # And the rest of the request, before git clone puts anything on disk.
    _refuse_bad_name(body.name)
    _validate_library_policy(body)
    warning = ""
    try:
        with _git_credentials(session, master_key, user, body.remote) as env:
            # Can this credential push here? A clone only reads (upload-pack),
            # so a read-only token cloned fine and an automatic policy then
            # recorded a failure after every commit (Codex, PR #19). Asked
            # before git clone runs, so a refusal leaves nothing behind.
            # A branch, not a tag: cloned, a tag is a detached HEAD that no
            # push can update (Codex, PR #19).
            problem = projects.branch_problem(body.remote, body.branch, env=env)
            if problem:
                raise HTTPException(status_code=400, detail=problem)
            refused = projects.probe_write(body.remote, env=env, branch=body.branch)
            if refused is not None and policy == "auto":
                raise HTTPException(
                    status_code=400,
                    detail=f"{_PROBE_SENTENCES[refused.status]} "
                    f"{autopush.scrub(refused.message, [])}".strip()
                    + (" Pushing cannot be automatic with this credential." if refused.status == "read_only" else ""),
                )
            if refused is not None and refused.status == "read_only":
                # Manual pushing over a read-only credential is a legitimate
                # way to follow a repository; it is said, not refused.
                warning = (
                    "This credential can read the repository but not write to it, so "
                    "pushes will fail until it has write access."
                )
            projects.clone(body.name, body.remote, body.branch, env=env)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    project = projectacl.create(session, user, body.name, remote=body.remote, branch=body.branch)
    project.push_policy = policy
    gitidentity.set_for_project(session, user, project, identity)
    if body.save_as_default:
        gitidentity.set_account_default(session, user, identity)
    _note_workspace_open(
        session, project.name, PROJECTS_ROOT / project.name,
        grants.create_project_key(session, project, user, master_key),
        user.id,
    )
    _record_policy(session, project, body, actor_id=user.id)
    activity.record(session, project, user, activity.IMPORTED, f"cloned from {body.remote}")
    return {"ok": True, "id": body.name, "warning": warning}


class InitBody(PolicyFields):
    """A new project, and everything it needs to not lose work on day one.

    Every field past the name is checked here with a sentence rather than left
    to pydantic, whose 422 for a missing field reads as a bug in the client.
    Nothing has a default that would stand in for a choice: no remote means no
    project, and no push policy means no project.
    """

    name: str
    #: Where the project's history goes. Required: a project whose only copy is
    #: this server is one disk failure from gone. Must be empty — an existing
    #: repository is brought in with Clone, not overwritten.
    remote: str = ""
    #: auto | manual. See autopush.
    push_policy: str | None = None
    #: The creator's commit identity for this project.
    git_name: str = ""
    git_email: str = ""
    #: Also make it the account default, used by projects with none of their own.
    save_as_default: bool = False
    #: A new HTTPS token for the remote's host, saved into this user's sealed
    #: git credential store (gitstore) if the project is created. Never stored
    #: anywhere else, never echoed back. Absent: an already-saved credential
    #: for the host is used, or none (an SSH remote with the deploy's key).
    token: str | None = None
    token_username: str | None = None


class RemoteCheckBody(BaseModel):
    url: str
    #: A token typed but not yet saved, tried without being stored.
    token: str | None = None
    token_username: str | None = None


def _remote_probe(
    session: Session, master_key: bytes, user: User, url: str,
    token: str | None, token_username: str | None,
):
    """Try ``url`` with the token given, or the user's saved credential for its
    host. Returns (probe, credential name or None, the secret to scrub, the
    credential row — saved or transient — or None), the row so that a real
    push can be made with exactly the credential that was probed.

    The URL is validated first, before any credential is chosen for it — the
    same order clone keeps. A token is only for HTTPS: over SSH the credential
    is a key, saved under Account & app settings, and a token typed against an
    ssh:// URL would be ignored, which reads as accepted.
    """
    from . import gitstore

    url = check_remote_url(url)
    token = (token or "").strip() or None
    if token is not None:
        if not url.lower().startswith("https://"):
            raise ProjectError(
                "a token is for an https:// remote — for an SSH remote, save an SSH key "
                "under Account & app settings → Git endpoints/accounts"
            )
        host = gitstore.authority_of(url)
        if host is None:
            raise ProjectError("this remote names no host for a token to be used with")
        row = gitstore.transient(master_key, host=host, secret=token, username=token_username)
        name = None
    else:
        try:
            row = gitstore.for_remote(session, user, url)
        except gitstore.GitStoreError as exc:
            raise ProjectError(str(exc)) from exc
        name = row.name if row is not None else None
    if row is None:
        return projects.probe_remote(url), None, None, None
    with gitstore.lease(master_key, row) as cred:
        probe = projects.probe_remote(url, env=cred.env)
    return probe, name, token, row


_PROBE_SENTENCES = {
    "ok": "Connected.",
    "auth_failed": "The remote refused the credential — check the token and its permissions.",
    "not_found": "No repository was found at that URL, or the credential cannot see it.",
    "read_only": (
        "The credential can read this repository but not write to it — the token needs "
        "write access (GitHub: Contents: Read and write; GitLab: write_repository)."
    ),
    "error": "The remote could not be reached.",
}

#: What creating a project over a repository with history says instead.
_NOT_EMPTY = (
    "that repository already has commits. A new project needs an empty one (no "
    "README, licence or .gitignore). To work on an existing repository, use Clone "
    "git remote in this dialog instead; to bring in files from your computer, use "
    "Import files."
)


@app.post("/api/projects/check-remote")
def check_remote(
    body: RemoteCheckBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Test connection: can this URL be reached, with which credential, and is
    it empty. Nothing is saved. The token, if one was typed, is not repeated in
    any part of the answer — git's message is redacted and the literal secret
    scrubbed from it besides."""
    try:
        probe, name, secret, _ = _remote_probe(
            session, master_key, user, body.url, body.token, body.token_username
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=autopush.scrub(str(exc), [body.token or ""]))
    detail = autopush.scrub(probe.message, [secret or ""])
    return {
        "status": probe.status,
        "ok": probe.status == "ok",
        # From the read result alone, whatever the write check said: a remote
        # with history cannot become a new project however writable it is,
        # and a read-only credential must not hide that it has history.
        "empty": probe.empty if probe.can_read else None,
        "writable": probe.writable,
        "message": (
            _NOT_EMPTY if probe.can_read and not probe.empty
            else _PROBE_SENTENCES[probe.status]
        ),
        "detail": detail,
        #: Which saved credential was used, when one matched. The UI shows it
        #: so "Connected" says *as whom*.
        "credential": name,
    }


class CredentialForBody(BaseModel):
    url: str


@app.post("/api/projects/credential-for")
def credential_for_remote(
    body: CredentialForBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Which of the caller's saved credentials a push to ``url`` would use.

    Asked of the server rather than worked out in the browser. The dialog
    used to compare its own parse of the URL's host with each row's stored
    spelling, while gitstore.for_remote normalises both — a legacy row saved
    as ``git.example:443``, or ``[2001:db8::1]:8443`` against the browser's
    unbracketed form, matched here and not there, and the dialog demanded a
    token the push would never have used (Codex, PR #19). One matcher now
    answers both. A URL that does not parse yet (it is being typed) is an
    empty answer, not an error. Names and hosts only, never a secret.
    """
    from . import gitstore

    try:
        url = check_remote_url(body.url)
    except ProjectError:
        return {"credential": None, "host": None, "https": False, "problem": ""}
    try:
        row = gitstore.for_remote(session, user, url)
    except gitstore.GitStoreError as exc:
        return {"credential": None, "host": gitstore.authority_of(url), "https": False,
                "problem": str(exc)}
    return {
        "credential": row.name if row is not None else None,
        "host": gitstore.authority_of(url),
        "https": url.lower().startswith("https://"),
        "problem": "",
    }


@app.get("/api/projects/new/defaults")
def new_project_defaults(
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """What the new-project dialog prefills: the account's commit identity, or
    failing that the sign-in email to start from, and the warning shown when
    manual pushing is chosen — from here, so it is one text everywhere."""
    mine = gitidentity.account_default(user)
    return {
        "identity": mine.as_json() if mine else {"name": "", "email": user.email or ""},
        "identity_is_default": mine is not None,
        "manual_push_warning": autopush.MANUAL_WARNING,
    }


def _validate_library_policy(decl) -> dict:
    """The library declaration's values, or the 400 that says what is wrong.

    Separate from _record_policy so the creation routes can ask it first,
    before anything leaves this server: called only at the end, an invalid
    value was refused after a clone, an init, or a project's first push had
    already happened, and the rollback could undo the database but not those
    (Codex, PR #19).
    """
    from . import library_policy

    try:
        return library_policy.validate(decl.contribute, decl.consume, decl.unique_components)
    except library_policy.PolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _refuse_bad_name(name: str) -> None:
    """A name that cannot be a project directory, or one already on disk,
    refused before any side effect rather than by init or clone after one."""
    try:
        taken = projects.exists(name)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if taken:
        raise HTTPException(status_code=409, detail=f"the name {name!r} is already taken")


def _record_policy(session: Session, project, decl, *, actor_id: int) -> None:
    """Write a project's library declaration, at creation or on change.

    Validated rather than coerced: a value that is not one of the choices is a
    stale client or a bug, and quietly reading it as the default would hide that
    while appearing to work.

    The consent hash is only stored when the caller both ticked the box and the
    settings actually permit contributing. Recording agreement for a project
    that shares nothing would leave a record of consent nobody acted on, and it
    would silently become live the day somebody changed one dropdown.
    """
    from . import library_policy

    values = _validate_library_policy(decl)
    row = session.scalar(
        select(ProjectPolicy).where(ProjectPolicy.project_id == project.id)
    ) or ProjectPolicy(project_id=project.id)
    row.contribute = values["contribute"]
    row.consume = values["consume"]
    row.unique_components = values["unique_components"]
    shares = values["contribute"] != "never"
    if decl.consented and shares:
        row.consent_sha = library_policy.consent_sha()
        row.consent_at = datetime.now(timezone.utc)
    elif not shares:
        row.consent_sha = None
        row.consent_at = None
    session.add(row)
    session.flush()


@app.post("/api/projects/init")
def init_project(
    body: InitBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Create a new project backed by an empty remote, owned by its creator.

    Checked in full before anything touches the disk: the name, the remote URL,
    the push policy, the commit identity, then the remote itself, with the
    credential that will be used to push to it. A remote that cannot be
    reached, refuses the credential, or already has history is refused here —
    never force-pushed over, never merged into.
    """
    from . import gitstore

    _refuse_taken_name(session, body.name)
    if not body.remote.strip():
        raise HTTPException(
            status_code=400,
            detail="a remote repository URL is required — create an empty repository "
            "with your git provider first (see How do I set this up?)",
        )
    try:
        remote = check_remote_url(body.remote)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        policy = autopush.validate_policy(body.push_policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        identity = gitidentity.validate(body.git_name, body.git_email)
    except gitidentity.IdentityError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Everything that can be checked here is checked before the remote is
    # even asked, let alone pushed to (Codex, PR #19).
    _refuse_bad_name(body.name)
    _validate_library_policy(body)

    try:
        probe, _, secret, cred_row = _remote_probe(
            session, master_key, user, remote, body.token, body.token_username
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=autopush.scrub(str(exc), [body.token or ""]))
    warning = ""
    # Emptiness first, and from the read result alone: a credential that
    # cannot write must not make a non-empty remote look acceptable (Codex,
    # PR #19). A project created over existing history diverges from it at
    # once.
    if probe.can_read and not probe.empty:
        raise HTTPException(status_code=409, detail=_NOT_EMPTY)
    if probe.status == "read_only" and policy == "manual":
        # Manual pushing over a credential that cannot write is allowed — the
        # owner may fix the token before the first push — and said, as for a
        # manual clone. The probe is a dry run and cannot see branch
        # protection anyway (see Projects._probe_write).
        warning = (
            "This credential can read the repository but not write to it, so pushes "
            "will fail until it has write access."
        )
    elif probe.status != "ok":
        detail = autopush.scrub(probe.message, [secret or ""])
        raise HTTPException(
            status_code=400,
            detail=f"{_PROBE_SENTENCES[probe.status]} {detail}".strip(),
        )

    token = (body.token or "").strip()
    if token:
        try:
            # Into the one store git credentials live in, under the row that
            # already holds this host if there is one (a new token replaces
            # it: there is one credential per host).
            gitstore.put(
                session, master_key, user,
                name=gitstore.name_for(session, user, remote),
                host=gitstore.authority_of(remote),
                method="https_token", secret=token,
                username=(body.token_username or "").strip() or "git",
            )
        except gitstore.GitStoreError as exc:
            raise HTTPException(status_code=400, detail=autopush.scrub(str(exc), [token]))

    try:
        dest = projects.init_local(body.name, identity=identity)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        projects.set_remote(dest, remote)
    except ProjectError as exc:
        # A project without the remote it was created for is the local-only
        # project this dialog exists to stop making. Rolled back, as an import
        # that fails half-way is.
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc))

    if policy == "auto":
        # The real check. The probe above is a dry run, and a dry run cannot
        # see branch protection (a protected branch, a ruleset, a pre-receive
        # hook): those act on the update and the pack, which a dry run never
        # sends. So with automatic pushing the project's first push — its
        # genuine first commit, by the creator, which they asked to have
        # pushed — is made now, with the credential that was probed, and a
        # rejection refuses creation: the directory is removed, and raising
        # rolls back this request's session, so no project row and no newly
        # entered token are kept. Codex, PR #19.
        try:
            lease = gitstore.lease(master_key, cred_row) if cred_row is not None else nullcontext(None)
            with lease as leased:
                projects.push(
                    body.name, env=leased.env if leased is not None else None,
                    checkout=dest, url=remote,
                )
        except ProjectError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail="the remote refused the project's first push, so the project was not "
                f"created: {autopush.scrub(str(exc), [secret or ''])}",
            )

    # Past here a failure is not expected — everything that can be validated
    # has been — but if one happens after the first push, the local directory
    # is still removed, and the error says the branch was already published:
    # deleting someone's remote branch behind their back is not this route's
    # call, so it is reported for them to decide.
    try:
        project = projectacl.create(session, user, body.name, remote=remote)
        project.push_policy = policy
        gitidentity.set_for_project(session, user, project, identity)
        if body.save_as_default:
            gitidentity.set_account_default(session, user, identity)
        _note_workspace_open(
            session, project.name, PROJECTS_ROOT / project.name,
            grants.create_project_key(session, project, user, master_key),
            user.id,
        )
        _record_policy(session, project, body, actor_id=user.id)
        activity.record(session, project, user, activity.IMPORTED, "created empty")
        if policy == "auto":
            # Pushed above; recorded now that the member row exists.
            autopush.record(session, project.id, user.id, None)
    except Exception as exc:  # noqa: BLE001 — cleaned up, then reported
        shutil.rmtree(dest, ignore_errors=True)
        # The open-workspace registry is process memory, holding the path and
        # the project key: the request's rollback cannot reach it. Left, it
        # named a directory that no longer exists, kept the key, and the idle
        # sweeper would later try to seal nothing. The database side — the
        # project row, its grants, the open-workspace row — goes with the
        # rollback.
        workspaces.forget(body.name)
        if policy != "auto":
            raise
        reason = exc.detail if isinstance(exc, HTTPException) else "an internal error"
        logger.exception("creating %s failed after its first push", body.name)
        raise HTTPException(
            status_code=getattr(exc, "status_code", 500),
            detail=(
                f"the project could not be created ({reason}), but its first commit had "
                f"already been pushed to branch main of {autopush.scrub(remote, [])}. That "
                "branch has been left as it is: delete it there before trying again, or "
                "bring the repository in with Clone git remote."
            ),
        ) from exc
    return {"ok": True, "id": body.name, "warning": warning}


class ShareBody(BaseModel):
    email: str
    # The owner has seen the "this address has no account" screen and typed the
    # address again. Without it, an unknown address is refused with a 404 so the
    # confirmation cannot be skipped by a client that forgets to ask.
    confirmed_new_account: bool = False


@app.post("/api/projects/{project_id}/open")
def open_project(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Decrypt a project's files so they can be worked on.

    A deliberate act, and the only route that needs the project key: once a
    workspace is open, every other route reads ordinary files. That is what keeps
    the master key out of twenty-eight signatures.
    """
    project = _project_or_404(session, user, project_id)
    if not workspace.is_sealed(PROJECTS_ROOT, project_id):
        if workspace_files.enabled() and workspaces.key_for(project_id) is None:
            # Open, but this process holds no key to seal it with: its row was
            # lost, or never written. Take this member's copy now, so the idle
            # sweep can seal the project again (Codex, PR #20). With the agent
            # path only: on the mount, a restart drops a row only when the
            # directory is really gone.
            try:
                key = grants.project_key_for(session, project, user, master_key)
            except grants.NoGrant:
                key = None
            if key is not None:
                _note_workspace_open(session, project_id, PROJECTS_ROOT / project_id, key,
                                     user.id)
        _touch_workspace(session, project_id, user.id)
        _drop_placeholder_identity(project_id)
        return {"ok": True, "already_open": True}

    try:
        project_key = grants.project_key_for(session, project, user, master_key)
    except grants.NoGrant:
        # A member without a wrapped copy — possible for a project that predates
        # the key machinery, and worth naming rather than failing to decrypt.
        raise HTTPException(
            status_code=409,
            detail="you have no key for this project; ask its owner to re-share it",
        )
    sealer_errors = workspace_files.seal_errors() if workspace_files.enabled() else ()
    try:
        if workspace_files.enabled():
            workspace_files.unseal(project_id, project_key)
        else:
            with _seal_gate(project_id, sealing=False):
                workspace.unseal(PROJECTS_ROOT, project_id, project_key)
    except _GateBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (workspace.WorkspaceError, *sealer_errors) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    _note_workspace_open(session, project_id, PROJECTS_ROOT / project_id, project_key, user.id)
    _drop_placeholder_identity(project_id)
    activity.record(session, project, user, activity.OPENED, "")
    return {"ok": True, "already_open": False}


def _drop_placeholder_identity(project_id: str) -> None:
    """Clear the stand-in identity older versions wrote into this repository.

    On open, because that is the first moment a sealed repository can be
    reached at all — a migration run at boot finds sealed projects as
    ciphertext. Best-effort: a repository git cannot read is reported by every
    route that uses it, and failing the open over a config cleanup would only
    hide that behind a less useful error.
    """
    try:
        if projects.drop_placeholder_identity(projects.project_dir(project_id)):
            logger.info("removed the placeholder commit identity from %s", project_id)
    except (ProjectError, OSError, subprocess.SubprocessError):
        logger.exception("could not check %s for the placeholder identity", project_id)


@app.post("/api/projects/{project_id}/seal")
def seal_project(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Put a project back to sealed now, rather than waiting for the timeout."""
    _project_or_404(session, user, project_id)
    why: list[str] = []
    sealed = _seal_workspace(session, project_id, why, reason=f"sealed by {user.email}")
    # Not sealed because something is using it is said, not left as a bare
    # false: an operation in flight holds the project's gate, and the seal
    # gives up after a few seconds rather than waiting it out.
    return {"ok": True, "sealed": sealed, **({"busy": True, "detail": why[0]} if why else {})}


@contextlib.contextmanager
def _seal_gate(project_id: str, *, sealing: bool):
    """The project's gate (sandbox/gate.py), exclusive, around a seal or
    unseal done on the mount, when runs execute in run containers: they hold
    it shared for their whole run, so a seal waits for none of them and
    cannot archive files a stage is writing. Without workspaces nothing
    starts containers, and nothing is taken."""
    from .sandbox import gate
    from .sandbox import jobs as sandbox_jobs

    if not sandbox_jobs.enabled():
        yield
        return
    with gate.held(PROJECTS_ROOT, project_id, exclusive=True,
                   wait_s=gate.SEAL_WAIT_S if sealing else gate.START_WAIT_S):
        yield


def _seal_workspace(session: Session, project_id: str, why: list[str] | None = None, *,
                    gate_held: bool = False, reason: str = "") -> bool:
    """Seal one workspace, unless something is still using it.

    Every seal is recorded in the project's activity with ``reason``. Only
    unseals were, so how often and why a project re-locked could be told
    only from container logs, which a redeploy deletes.

    Refuses while a run is in flight, and that is not politeness: the worker is
    another container reading those files, and sealing would delete the project
    out from under a stage mid-write.
    """
    key = workspaces.key_for(project_id)
    if key is None:
        return False
    project = session.scalar(select(Project).where(Project.name == project_id))
    if project is not None:
        busy = session.scalar(
            select(Run).where(
                Run.project_id == project.id,
                Run.status.in_([runqueue.QUEUED, runqueue.RUNNING, runqueue.CANCELLING]),
            )
        )
        if busy is not None:
            if why is not None:
                why.append("a run is in progress in this project; seal it when the run "
                           "has finished")
            return False
    held = _kicad_desktop_holds(project_id)
    if held:
        # Sealing deletes the plaintext tree. A KiCad window with that tree
        # open then shows "Failed to get the working directory" over an empty
        # file list, and anything saved from it has nowhere to go. Editing in
        # the desktop touches nothing here, so an hour of routing there read
        # as an idle project and was sealed out from under it.
        workspaces.touch(project_id)
        if why is not None:
            why.append("it is open in the KiCad desktop; close it there first")
        return False
    # With workspaces behind the agent (phase 4), in a sealer container, after
    # the workspace is stopped and removed, so no container is left holding the
    # plaintext. Its failures are caught here as WorkspaceError's are; with the
    # flag off nothing else is.
    sealer_errors = workspace_files.seal_errors() if workspace_files.enabled() else ()
    try:
        if workspace_files.enabled():
            size = workspace_files.seal(project_id, key, gate_held=gate_held)
        else:
            with (contextlib.nullcontext() if gate_held
                  else _seal_gate(project_id, sealing=True)):
                size = workspace.seal(PROJECTS_ROOT, project_id, key)
    except (workspace.WorkspaceError, *sealer_errors, _GateBusy) as exc:
        logger.warning("could not seal %s: %s", project_id, exc)
        if why is not None:
            why.append(f"the project is in use ({exc}); try again shortly")
        return False
    workspaces.forget(project_id)
    # The row and the wrapped key it carries exist only while the project is
    # unsealed. It is sealed now, so both go.
    openstate.forget(session, project_id)
    logger.info("sealed %s (%d bytes): %s", project_id, size, reason or "no reason given")
    if project is not None:
        activity.record(session, project, None, activity.SEALED, reason)
    return True


@app.get("/api/projects/{project_id}/members")
def list_members(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Who can open this project. Any member may see the list — you are entitled
    to know who else can read what you are working on."""
    try:
        project = _project_or_404(session, user, project_id)
    except projectacl.NoSuchProject:
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    return {
        "owned_by_me": project.owner_id == user.id,
        "members": [
            {"id": u.id, "email": u.email, "role": role, "is_you": u.id == user.id}
            for u, role in projectacl.members(session, project)
        ],
        # Shown to every member, not just the owner: an outstanding invitation is
        # someone who is about to be able to read this, and that is worth seeing
        # before they arrive rather than after.
        "invited": [
            {
                "id": inv.id,
                "email": inv.invitee.email,
                "expires_at": inv.expires_at.isoformat(),
            }
            for inv in projectacl.outstanding(session, project)
        ],
    }


def _invitation_email(
    project_name: str, invited_by: str, expires, invitation_id: int, secret: str | None
) -> tuple[str, str]:
    """The notice an invitee gets.

    Two versions, because the recipients are in different situations. Someone
    with an account only needs telling; someone without needs a link, and needs
    to understand what that link is before they forward it to anyone.

    The secret rides in the URL *fragment*. Fragments are never sent to a server
    in a request line, so it stays out of access logs, Referer headers and
    anything sitting in front of the app — the page reads it and posts it back
    deliberately.
    """
    where = os.environ.get("BLPL_PUBLIC_URL", "").strip() or "your BLPL server"
    subject = f"{invited_by} shared the project \u201c{project_name}\u201d with you"

    if secret is None:
        return subject, f"""{invited_by} has invited you to the BLPL project "{project_name}".

Open {where} and sign in with this address to accept or decline. The invitation
is waiting for you on your dashboard.

You will not have access until you accept, and the invitation expires on
{expires:%d %B %Y}.

If you were not expecting this, you can ignore it — declining costs nothing and
nothing is shared with you in the meantime.
"""

    link = f"{where}/invite/{invitation_id}#{secret}"
    return subject, f"""{invited_by} has invited you to the BLPL project "{project_name}".

You do not have an account yet, so this link both creates one and accepts the
invitation:

{link}

THIS LINK EXPIRES IN 24 HOURS.

Please note: ANYONE WHO HAS THIS LINK CAN USE IT, so do not forward it. Creating
the account is limited to {invited_by}'s intended recipient — you will have to
enter a verification code sent to this address — but the link itself is the key
to the project, so treat it like one.

If you were not expecting this, ignore it. The invitation lapses on its own and
nothing is shared with you in the meantime.
"""


@app.post("/api/projects/{project_id}/members")
def add_member(
    project_id: str,
    body: ShareBody,
    background: BackgroundTasks,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Share a project with someone, by the email their account signed in with.

    They must already have signed in at least once. Inviting an address that has
    never been here would mean storing a pending grant against a string, and a
    grant that attaches to whoever later claims that address is a way to hand a
    project to the wrong person.
    """
    try:
        project = projectacl.require_owner(session, user, project_id)
    except projectacl.NoSuchProject:
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    except projectacl.NotTheOwner:
        raise HTTPException(status_code=403, detail="only the owner can share this project")

    address = body.email.strip().lower()
    target = session.scalar(select(User).where(User.email == address))
    if target is not None and target.id == user.id:
        raise HTTPException(status_code=400, detail="you already own this project")

    # Two paths, and which one applies is not the owner's choice — it depends on
    # whether the address already has a usable account.
    known = target is not None and grants.public_key_of(session, target) is not None
    if not known and not body.confirmed_new_account:
        # Refused rather than silently taking the weaker path. The owner is shown
        # what emailing a secret means and has to type the address again, so the
        # tradeoff is one they made rather than one that happened to them.
        raise HTTPException(
            status_code=404,
            detail=f"{address} has no account here yet",
        )

    secret = None
    wrapped = None
    if not known:
        target = users.placeholder_for(session, address)
        if grants.has_key(session, project):
            # No keypair exists to seal to, so the key is wrapped under a secret
            # that lives only in the emailed link. Weaker on purpose, and bounded
            # by a 24-hour life and by being destroyed on first use.
            secret = projectkey.new_invitation_secret()
            project_key = grants.project_key_for(session, project, user, master_key)
            wrapped = projectkey.seal_under_secret(secret, project_key)

    try:
        invitation = projectacl.invite(
            session,
            project,
            user,
            target,
            ttl=projectacl.SECRET_INVITATION_TTL if not known else None,
            wrapped_key=wrapped,
        )
    except projectacl.AlreadyAMember:
        raise HTTPException(status_code=409, detail=f"{address} can already open this project")

    # For someone who already has a key, seal straight to it — strictly better
    # than a secret in an email, and it needs no link.
    if known and grants.has_key(session, project):
        project_key = grants.project_key_for(session, project, user, master_key)
        grants.grant_to(session, project, target, project_key)
    # Sent in the background, and failure is logged rather than raised. A relay
    # being briefly unreachable must not roll back the invitation the owner just
    # made — they would see an error and assume nothing happened, while the
    # recipient may already have it.
    if target.email:
        subject, body_text = _invitation_email(
            project.name, user.email or "Someone", invitation.expires_at, invitation.id, secret
        )
        background.add_task(mailer.send, target.email, subject, body_text)

    activity.record(session, project, user, activity.SHARED, f"invited {target.email}")
    return {
        "ok": True,
        "invited": target.email,
        "invitation_id": invitation.id,
        "expires_at": invitation.expires_at.isoformat(),
        # So the UI can say which kind of invitation went out. They differ in
        # what the recipient has to do and in how long they have to do it.
        "new_account": not known,
        # So the UI can say "they have been emailed" or "tell them yourself"
        # instead of implying a notice that was never sent.
        "emailed": bool(target.email) and mailer.configured(),
    }


@app.delete("/api/projects/{project_id}/members/{user_id}")
def remove_member(
    project_id: str,
    user_id: int,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    try:
        project = projectacl.require_owner(session, user, project_id)
    except projectacl.NoSuchProject:
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    except projectacl.NotTheOwner:
        raise HTTPException(status_code=403, detail="only the owner can change who has access")
    try:
        removed = projectacl.unshare(session, project, user_id)
        grants.revoke_grant(session, project, user_id)
        # Their checkout goes; their branch stays. Being removed from a project
        # should not also mean losing unmerged work.
        try:
            if workspace_files.enabled():
                # Through the agent: without the project mount, the checkout is
                # not visible here, and "nothing to remove" left a removed
                # member's uncommitted work on disk (Codex, PR #20).
                workspace_files.remove_worktree(project.name, user_id)
            else:
                worktrees.remove(PROJECTS_ROOT, project.name, user_id)
        except (ProjectError, HTTPException, workspace_files.AgentCallError) as exc:
            logger.warning("could not remove worktree for %s: %s", project.name, exc)
    except projectacl.NotTheOwner as exc:
        # Removing the owner would strand the project: nobody left who can
        # share it, delete it, or grant anyone else access.
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "removed": removed}


@app.delete("/api/projects/{project_id}/invitations/{invitation_id}")
def revoke_invitation(
    project_id: str,
    invitation_id: int,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Withdraw an offer before it is taken."""
    try:
        project = projectacl.require_owner(session, user, project_id)
    except projectacl.NoSuchProject:
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    except projectacl.NotTheOwner:
        raise HTTPException(status_code=403, detail="only the owner can withdraw an invitation")
    revoked = projectacl.revoke(session, project, invitation_id)
    if revoked:
        # The wrapped key went out with the invitation; withdrawing it has to take
        # that back too, or a declined invitee still holds a readable copy.
        invitee_id = session.scalar(
            select(ProjectInvitation.invitee_id).where(ProjectInvitation.id == invitation_id)
        )
        if invitee_id is not None:
            grants.revoke_grant(session, project, invitee_id)
    return {"ok": True, "revoked": revoked}


@app.get("/api/invitations")
def list_invitations(
    user: User = Depends(require_onboarded), session: Session = Depends(session_scope)
) -> list[dict]:
    """Projects waiting for this user to say yes or no.

    Not under /api/projects/{id}: the whole point is that you cannot reach that
    project yet, so a route beneath it would 404 on the permission check that has
    not been granted.
    """
    return [
        {
            "id": inv.id,
            "project": inv.project.name,
            "invited_by": inv.invited_by.email,
            "expires_at": inv.expires_at.isoformat(),
        }
        for inv in projectacl.pending_for(session, user)
    ]


@app.post("/api/invitations/{invitation_id}/accept")
def accept_invitation(
    invitation_id: int,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    try:
        project = projectacl.accept(session, user, invitation_id)
        activity.record(session, project, user, activity.JOINED, "accepted an invitation")
    except projectacl.NoSuchInvitation:
        # Covers withdrawn, already answered, and expired alike. They are all
        # "there is nothing here for you to accept", and distinguishing them
        # would report on a project the caller still cannot see.
        raise HTTPException(status_code=404, detail="no invitation waiting for you")
    return {"ok": True, "project": project.name}


class RedeemBody(BaseModel):
    secret: str


@app.get("/api/invitations/{invitation_id}/preview")
def preview_invitation(invitation_id: int, session: Session = Depends(session_scope)) -> dict:
    """What an invitation link points at, before anyone signs in.

    Unauthenticated on purpose: the person following it has, by construction, no
    account yet. It reveals only the project name and who sent it — enough to
    decide whether to go on, and nothing that identifies the invitee, so a
    guessed id tells a stranger nothing about who was invited.
    """
    row = projectacl.live_by_id(session, invitation_id)
    if row is None:
        return {"valid": False}
    return {
        "valid": True,
        "project": row.project.name,
        "invited_by": row.invited_by.email,
        "expires_at": row.expires_at.isoformat(),
        "needs_secret": row.key_ciphertext is not None,
    }


@app.post("/api/invitations/{invitation_id}/redeem")
def redeem_invitation(
    invitation_id: int,
    body: RedeemBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Accept an invitation that arrived as a link, with its secret.

    Three things must all hold, and the order matters. The invitation must be
    live; the caller's *verified* address must be the one invited, so a leaked
    link cannot be redeemed by whoever finds it into an account of their own; and
    the secret must open the wrapped key, which is what proves they hold the link
    rather than merely knowing an id.
    """
    row = projectacl.live_by_id(session, invitation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="this invitation is no longer valid")

    invited_address = (row.invitee.email or "").strip().lower()
    if not user.email or user.email.strip().lower() != invited_address:
        # The link is not a bearer token for the project. It carries the key, but
        # the address it was sent to is still who it is for, and Clerk verified
        # that address at sign-up.
        raise HTTPException(
            status_code=403,
            detail=f"this invitation was sent to {invited_address}; you are signed in as {user.email}",
        )

    if row.key_ciphertext is not None:
        try:
            project_key = projectkey.open_with_secret(
                body.secret, row.key_nonce, row.key_ciphertext
            )
        except projectkey.GrantError:
            raise HTTPException(status_code=403, detail="that invitation link is not valid")
        # Re-sealed to a key only this account holds, before the secret-wrapped
        # copy is destroyed by accept(). From here on the link is worthless.
        grants.ensure_keypair(session, user, master_key)
        grants.grant_to(session, row.project, user, project_key)

    project = projectacl.accept(session, user, invitation_id)
    activity.record(session, project, user, activity.JOINED, "accepted an invitation")
    return {"ok": True, "project": project.name}


@app.post("/api/invitations/{invitation_id}/decline")
def decline_invitation(
    invitation_id: int,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    try:
        project = projectacl.decline(session, user, invitation_id)
    except projectacl.NoSuchInvitation:
        raise HTTPException(status_code=404, detail="no invitation waiting for you")
    # Give back the key that came with the offer. Saying no should leave you with
    # no more than you had before it arrived.
    grants.revoke_grant(session, project, user.id)
    return {"ok": True}


@app.post("/api/projects/import")
async def import_project(
    name: str = Form(...),
    files: list[UploadFile] = File(...),
    # Same declaration as the other two creation paths, as form fields because
    # this one is multipart. Closed when absent, for the same reason.
    contribute: str = Form("never"),
    consume: str = Form("ask"),
    unique_components: str = Form("never_contribute"),
    consented: bool = Form(False),
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
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

    # The declaration, checked before the import's commit is made.
    declaration = PolicyFields(
        contribute=contribute, consume=consume,
        unique_components=unique_components, consented=consented,
    )
    _validate_library_policy(declaration)
    # The account's identity: there is no project yet to hold one of its own,
    # and the import is a commit, so without one the import is refused before
    # anything is written (init_local says where to set it).
    identity = gitidentity.account_default(user)
    try:
        dest = projects.init_local(name, identity=identity)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        for f in imported:
            target = dest / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.data)
        projects.commit_all(name, f"Import {len(imported)} files via app upload", identity=identity)
    except Exception:
        # A half-written project would show up in every listing looking real.
        # Roll the directory back and let the error surface.
        shutil.rmtree(dest, ignore_errors=True)
        raise

    project = projectacl.create(session, user, name)
    _note_workspace_open(
        session, project.name, PROJECTS_ROOT / project.name,
        grants.create_project_key(session, project, user, master_key),
        user.id,
    )
    _record_policy(
        session, project,
        PolicyFields(contribute=contribute, consume=consume,
                     unique_components=unique_components, consented=consented),
        actor_id=user.id,
    )
    activity.record(session, project, user, activity.IMPORTED, f"{len(imported)} files uploaded")
    return {
        "ok": True,
        "id": name,
        "imported": len(imported),
        "files": [str(f.path) for f in imported],
        "skipped": skipped_out,
    }


@app.get("/api/projects/{project_id}/git/status")
def git_status(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> dict:
    checkout = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    project = _project_or_404(session, user, project_id)
    try:
        st = (workspace_files.git_status(checkout) if workspace_files.enabled()
              else projects.status(project_id, checkout=checkout))
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    member = gitidentity.membership(session, user, project)
    identity = gitidentity.resolve(session, user, project)
    return {
        "branch": st.branch, "ahead": st.ahead, "behind": st.behind,
        "dirty": st.dirty, "has_remote": st.has_remote,
        "remote_name": st.remote_name, "remote_url": st.remote_url,
        "unpublished": st.unpublished,
        "remote_problem": st.remote_problem,
        # auto | manual | null (never chosen; behaves as manual).
        "push_policy": project.push_policy,
        # Minutes idle before the project is sealed again; null is the default.
        "idle_seal_minutes": project.idle_seal_minutes,
        "idle_seal_default": idlepolicy.default_minutes(),
        # This member's last push, automatic or not: a failure stays here, and
        # in the strip, until a later push succeeds.
        "last_push": autopush.last_push(member),
        # Whether a commit by this user would be refused, so the strip can say
        # so before they try rather than after a save.
        "has_identity": identity is not None,
    }


class RemoteBody(BaseModel):
    url: str


@app.put("/api/projects/{project_id}/git/remote")
def set_git_remote(
    project_id: str,
    body: RemoteBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Point a project at a remote. Owner only.

    Where Push sends every member's work is the owner's decision, the same as
    who may see the project. The URL passes the same check as cloning —
    https://, ssh:// or user@host:path, never a path on this server.
    """
    try:
        projectacl.require_owner(session, user, project_id)
    except projectacl.NoSuchProject:
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    except projectacl.NotTheOwner:
        raise HTTPException(status_code=403, detail="only the owner can set this project's remote")
    checkout = _project_dir(session, user, project_id,
                            agent_ready=workspace_files.enabled())
    try:
        if workspace_files.enabled():
            workspace_files.set_remote(checkout, body.url)
            st = workspace_files.git_status(checkout)
        else:
            projects.set_remote(checkout, body.url)
            st = projects.status(project_id, checkout=checkout)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "remote_name": st.remote_name, "remote_url": st.remote_url}


class IdentityBody(BaseModel):
    name: str = ""
    email: str = ""


def _identity_json(identity) -> dict | None:
    return identity.as_json() if identity is not None else None


@app.get("/api/me/git-identity")
def get_account_identity(user: User = Depends(require_onboarded)) -> dict:
    """The commit identity used wherever a project has none of this user's own."""
    return {"identity": _identity_json(gitidentity.account_default(user)), "email": user.email}


@app.put("/api/me/git-identity")
def put_account_identity(
    body: IdentityBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    try:
        identity = gitidentity.validate(body.name, body.email)
    except gitidentity.IdentityError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    gitidentity.set_account_default(session, user, identity)
    return {"ok": True, "identity": identity.as_json()}


@app.delete("/api/me/git-identity")
def delete_account_identity(
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Clear the default. Projects with an identity of their own are unaffected;
    the rest refuse to commit for this user until one is set again."""
    gitidentity.set_account_default(session, user, None)
    return {"ok": True}


@app.get("/api/projects/{project_id}/git/identity")
def get_project_identity(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """This member's identity for the project, the account default it falls
    back to, and which of the two a commit would use. Only the caller's own:
    nothing here reads or writes another member's."""
    project = _project_or_404(session, user, project_id)
    return {
        "project": _identity_json(gitidentity.for_project(session, user, project)),
        "default": _identity_json(gitidentity.account_default(user)),
        "effective": _identity_json(gitidentity.resolve(session, user, project)),
    }


@app.put("/api/projects/{project_id}/git/identity")
def put_project_identity(
    project_id: str,
    body: IdentityBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Set the caller's identity for this project. Any member may set their own;
    a blank name and email clears it, falling back to the account default."""
    project = _project_or_404(session, user, project_id)
    if not body.name.strip() and not body.email.strip():
        gitidentity.set_for_project(session, user, project, None)
        return {"ok": True, "project": None}
    try:
        identity = gitidentity.validate(body.name, body.email)
    except gitidentity.IdentityError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    gitidentity.set_for_project(session, user, project, identity)
    return {"ok": True, "project": identity.as_json()}


class CredentialBody(BaseModel):
    token: str = ""
    username: str | None = None


@app.get("/api/projects/{project_id}/git/credential")
def get_project_credential(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Which of the caller's saved credentials this project's pushes would use.

    Credentials are per user and matched by the remote's host, so this is a
    question about the caller, not the project: two members pushing to the
    same remote each use their own. Names and hosts only, as the settings
    listing gives them — never a secret.
    """
    from . import gitstore

    # Through the agent with the agent path on, as pull and push resolve it:
    # with the mount absent, reading the remote from disk answered 503
    # (Codex, PR #20).
    via = workspace_files.enabled()
    checkout = _project_dir(session, user, project_id, agent_ready=via)
    try:
        url = (workspace_files.remote_of(checkout, "push") if via
               else projects.remote_of(project_id, operation="push", checkout=checkout))
    except ProjectError as exc:
        return {"host": None, "credential": None, "method": None, "https": False, "problem": str(exc)}
    host = gitstore.authority_of(url) if url else None
    try:
        row = gitstore.for_remote(session, user, url) if url else None
    except gitstore.GitStoreError as exc:
        return {"host": host, "credential": None, "method": None, "https": False, "problem": str(exc)}
    return {
        "host": host,
        "credential": row.name if row is not None else None,
        "method": row.method if row is not None else None,
        "https": url.lower().startswith("https://"),
        "problem": "",
    }


@app.put("/api/projects/{project_id}/git/credential")
def put_project_credential(
    project_id: str,
    body: CredentialBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Save a new HTTPS token for this project's remote host, into the caller's
    own credential store — the same rows Account & app settings → Git
    endpoints/accounts lists, under the row that already holds the host when
    there is one. Any member may, for themselves. Nothing is echoed back."""
    from . import gitstore

    # Through the agent with the agent path on, as pull and push resolve it:
    # with the mount absent, reading the remote from disk answered 503
    # (Codex, PR #20).
    via = workspace_files.enabled()
    checkout = _project_dir(session, user, project_id, agent_ready=via)
    token = body.token.strip()
    try:
        url = (workspace_files.remote_of(checkout, "push") if via
               else projects.remote_of(project_id, operation="push", checkout=checkout))
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not url:
        raise HTTPException(status_code=400, detail="this project has no remote to save a credential for")
    if not url.lower().startswith("https://"):
        raise HTTPException(
            status_code=400,
            detail="a token is for an https:// remote — for SSH, save a key under "
            "Account & app settings → Git endpoints/accounts",
        )
    try:
        name = gitstore.name_for(session, user, url)
        gitstore.put(
            session, master_key, user, name=name, host=gitstore.authority_of(url),
            method="https_token", secret=token, username=(body.username or "").strip() or "git",
        )
    except gitstore.GitStoreError as exc:
        raise HTTPException(status_code=400, detail=autopush.scrub(str(exc), [token]))
    return {"ok": True, "credential": name}


class PushPolicyBody(BaseModel):
    policy: str | None = None


@app.put("/api/projects/{project_id}/git/push-policy")
def put_push_policy(
    project_id: str,
    body: PushPolicyBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Push after every commit, or only by hand. Owner only, for the reason the
    remote is: it decides where every member's work goes and when, and whether
    unpushed work may sit on this server alone is the owner's risk to take."""
    try:
        project = projectacl.require_owner(session, user, project_id)
    except projectacl.NoSuchProject:
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    except projectacl.NotTheOwner:
        raise HTTPException(
            status_code=403, detail="only the owner can change this project's push policy"
        )
    try:
        project.push_policy = autopush.validate_policy(body.policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    session.flush()
    return {
        "ok": True,
        "push_policy": project.push_policy,
        "warning": autopush.MANUAL_WARNING if project.push_policy == "manual" else "",
    }


class IdleSealBody(BaseModel):
    minutes: int | None = None


@app.put("/api/projects/{project_id}/idle-seal")
def put_idle_seal(
    project_id: str,
    body: IdleSealBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """How long the project may sit idle before it is sealed again. Owner
    only: it decides how long every member's files stay decrypted on the
    server after the last of them stops working. Null restores the default."""
    try:
        project = projectacl.require_owner(session, user, project_id)
    except projectacl.NoSuchProject:
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    except projectacl.NotTheOwner:
        raise HTTPException(
            status_code=403, detail="only the owner can change how long this project stays open"
        )
    try:
        project.idle_seal_minutes = idlepolicy.validate(body.minutes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    session.flush()
    return {
        "ok": True,
        "idle_seal_minutes": project.idle_seal_minutes,
        "effective_minutes": project.idle_seal_minutes or idlepolicy.default_minutes(),
    }


class CommitBody(BaseModel):
    message: str


@app.post("/api/projects/{project_id}/git/commit")
def git_commit(project_id: str, body: CommitBody, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes | None = Depends(optional_master_key)) -> dict:
    project = _project_or_404(session, user, project_id)
    checkout = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    try:
        out, scheduled = _commit_and_push(session, user, project, checkout, body.message, master_key)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # auto_push: an automatic push of this commit is on its way, so the
    # client's Commit + Push must not push it again. A second push of the same
    # commit could block the dialog behind the lock, and a transient failure of
    # it would overwrite the automatic push's success (Codex, PR #19).
    return {
        "ok": True, "committed": out is not None,
        "auto_push": scheduled is not None, "push_seq": scheduled,
        # How long the client should wait for that push before treating it
        # as slow: the server's own worst case, so the two cannot drift.
        "push_wait_s": autopush.PUSH_WAIT_SECONDS if scheduled is not None else None,
    }


@app.get("/api/projects/{project_id}/git/diff")
def git_diff(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> dict:
    """What changed in the working copy since the last commit — the 'what did that
    run do' view. Diffs are size-capped server-side."""
    checkout = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    try:
        if workspace_files.enabled():
            return workspace_files.git_diff(checkout)
        return projects.diff(project_id, checkout=checkout)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/projects/{project_id}/git/pull")
def git_pull(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key)) -> dict:
    checkout = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    try:
        # A pull fetches, so the credential is the fetch URL's — which need not
        # be the host a push goes to.
        # Resolved once: the credential is leased for this URL and git is
        # handed this URL, so the two cannot diverge if origin changes between.
        via = workspace_files.enabled()
        dest = (workspace_files.remote_of(checkout, "fetch") if via
                else projects.remote_of(project_id, operation="fetch", checkout=checkout))
        with _git_credentials(session, master_key, user, dest) as env:
            out = (workspace_files.pull(checkout, dest, env) if via
                   else projects.pull(project_id, env=env, checkout=checkout, url=dest))
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "output": out}


@app.post("/api/projects/{project_id}/git/push")
def git_push(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key)) -> dict:
    # The checkout /git/commit just committed in, so "Commit + Push" pushes
    # the commit it made rather than the owner's branch.
    checkout = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    project = _project_or_404(session, user, project_id)
    project_id_, user_id = project.id, user.id
    recorded = False
    # What this push can answer for, counted before it chooses its commit
    # (inside push, under the lock): a save that commits after that point
    # draws a newer number and gets its own automatic push (see record).
    answers_for = autopush.requested_now(project_id_, user_id)

    def after(exc: ProjectError | None) -> None:
        # Recorded as an automatic push's outcome is — under the origin lock,
        # in a session of its own committed before the lock goes — so the
        # strip shows one state whichever way the push was started, and an
        # overlapping automatic push cannot record out of order with it. A
        # success clears a failure an automatic push left behind.
        nonlocal recorded
        recorded = True
        autopush.record_now(
            project_id_, user_id, None if exc is None else autopush.scrub(str(exc), []),
            seq=answers_for,
        )

    try:
        via = workspace_files.enabled()
        dest = (workspace_files.remote_of(checkout, "push") if via
                else projects.remote_of(project_id, operation="push", checkout=checkout))
        with _git_credentials(session, master_key, user, dest) as env:
            out = (workspace_files.push(checkout, dest, env, after=after) if via
                   else projects.push(project_id, env=env, checkout=checkout, url=dest,
                                      after=after))
    except ProjectError as exc:
        if not recorded:
            autopush.record_now(
                project_id_, user_id, autopush.scrub(str(exc), []), seq=answers_for
            )
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "output": out}


# The files a user edits in the browser: design markdown and the project config.
# Deliberately NOT everything — generated artifacts live in .pipeline/ and are
# read through the artifacts endpoint, and .git is off-limits. Editing is scoped
# to the design inputs.
# Diagrams are design documents: text, versioned, reviewed as a diff. A block
# diagram written as mermaid is worth more than an SVG somebody drew, because
# it can be read in a pull request and corrected in a sentence.
_EDITABLE_SUFFIXES = {".md", ".yaml", ".yml", ".mmd", ".mermaid"}


def _editable_file(session: Session, user: User, project_id: str, name: str) -> Path:
    """Resolve a client-supplied path to an editable file inside the project.

    Multi-board projects put each board's design document in its own directory,
    so ``base/design.md`` has to be addressable — a root-only rule made every
    board's actual design document unopenable in the workbench.

    Widening *what* may be named does not widen *where* it may land. The path is
    held to: no backslashes, not absolute, no segment that is ``..`` or begins
    with a dot, an editable suffix, and — after resolving symlinks — a location
    still inside the project root. The resolve-then-contain check is the one
    that matters; the segment rules just refuse the obvious cases early with a
    clearer error.
    """
    proj = _project_dir(session, user, project_id)
    parts = _editable_rel(name).split("/")
    target = proj.joinpath(*parts).resolve()
    root = proj.resolve()
    if not target.is_relative_to(root) or target == root:
        raise HTTPException(status_code=400, detail="invalid filename")
    return target


def _editable_rel(name: str) -> str:
    """The lexical half of ``_editable_file``: the rules a name must pass before
    anything is resolved, returned as the normalised relative path. With files
    going through the workspace agent, the containment half is the agent's,
    which follows no link at all."""
    parts = [seg for seg in name.split("/") if seg]
    if any(seg.startswith(".") and seg != ".." for seg in parts):
        # The refusal is deliberate, and "invalid filename" taught nobody why:
        # a user trying to fix a wrong requested_symbol edited .pipeline/hdm.yaml
        # in the workbench, got the generic error, and went looking for the
        # cause in container logs. The dot-directories hold generated artifacts
        # — an edit there is overwritten by the next run of the stage that
        # writes it, so the honest answer names the file that actually holds
        # the fact.
        raise HTTPException(
            status_code=400,
            detail=(
                "files under dot-directories are pipeline artifacts — regenerated "
                "on every run, so an edit here would not survive. Change the "
                "design markdown (it is the source of every artifact) and rerun "
                "the stage instead."
            ),
        )
    if (
        not parts
        or "\\" in name
        or name.startswith("/")
        or any(seg == ".." for seg in parts)
    ):
        raise HTTPException(status_code=400, detail="invalid filename")
    if Path(parts[-1]).suffix.lower() not in _EDITABLE_SUFFIXES:
        raise HTTPException(status_code=400, detail="invalid filename")
    return "/".join(parts)


class FileBody(BaseModel):
    content: str


def _resolve_board(proj: Path, requested: str | None) -> str | None:
    """Which board a request is about, or None for a single-board project.

    Mirrors blpl.core.cli._board so the app and the command line refuse the same
    things for the same reasons. Guessing is the failure to avoid: picking a
    board would run the wrong one and write a perfectly plausible artifact that
    nobody would think to question.
    """
    try:
        man = (workspace_files.manifest(proj) if workspace_files.enabled()
               else project_manifest.discover(proj))
    except project_manifest.ManifestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if man.implicit:
        if requested and requested != man.boards[0].name:
            raise HTTPException(
                status_code=400,
                detail=f"{proj.name} is a single-board project; it has no board {requested!r}",
            )
        return None
    if not requested:
        names = ", ".join(b.name for b in man.boards)
        raise HTTPException(
            status_code=400,
            detail=f"{proj.name} has more than one board ({names}); say which with ?board=",
        )
    if man.board(requested) is None:
        names = ", ".join(b.name for b in man.boards)
        raise HTTPException(
            status_code=404, detail=f"no board {requested!r} in {proj.name}. Known: {names}"
        )
    return requested


@app.get("/api/projects/{project_id}/boards")
def get_boards(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """The project's boards, how they mate, and which combinations are built.

    Always answers, even for the projects that predate multi-board: one with no
    ``project.md`` reports a single implicit board named after itself, so the UI
    has one shape to render and nothing existing had to be migrated to get it.
    """
    proj = _project_dir(session, user, project_id)
    try:
        man = project_manifest.discover(proj, project_id=project_id)
    except project_manifest.ManifestError as exc:
        # A malformed manifest should cost you the board list, not the project.
        return {
            "project_id": project_id,
            "schema_version": 1,
            "implicit": True,
            "boards": [],
            "mates": [],
            "configurations": [],
            "warnings": [str(exc)],
        }
    return man.to_dict()


class ProjectRoutingBody(BaseModel):
    """Per-task overrides for one project. A task absent here has no override."""

    tasks: dict[str, list[str]]


@app.get("/api/projects/{project_id}/routing")
def get_project_routing(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """This project's task routing: the account default, and what overrides it.

    Both, separately, rather than the merged result. A merged view cannot say
    which tasks are actually overridden — a chain that happens to match the
    default is indistinguishable from one that was never set — and that
    difference is exactly what the "use the account default" control acts on.
    """
    project = _project_or_404(session, user, project_id)
    defaults = llmconfig.load(session, user)
    return {
        "project_id": project_id,
        "tasks": list(appconfig.KNOWN_TASKS),
        "endpoints": sorted(defaults.endpoints),
        "defaults": {t: list(c) for t, c in defaults.tasks.items()},
        "overrides": llmconfig.project_overrides(session, user, project.id),
    }


@app.put("/api/projects/{project_id}/routing")
def put_project_routing(
    project_id: str,
    body: ProjectRoutingBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Replace this project's overrides.

    Whole set rather than one task, for the same reason the settings screen
    sends the whole registry: a merge makes "remove this override" impossible
    to express. A task with an empty chain is treated as no override, so
    clearing one is simply sending it empty or leaving it out.

    Overrides are per user as well as per project. Two members of a project
    have their own endpoints and their own keys, so a route one of them
    declares cannot mean anything for the other.
    """
    project = _project_or_404(session, user, project_id)
    try:
        llmconfig.save_project_overrides(session, user, project.id, body.tasks)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ok": True}


class BoardConfigBody(BaseModel):
    """A whole board config, as the schema describes it.

    Whole rather than patched: a partial update would have to merge, and
    merging into a file someone may have hand-edited between the GET and the
    PUT is how a field nobody touched changes value. The client sends back what
    it was given, with its edits.
    """

    config: dict


@app.get("/api/projects/{project_id}/boards/{board}/config")
def get_board_config(
    project_id: str,
    board: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """One board's project.yaml, as structure rather than as text.

    The only way to change a board's outline, stackup or net classes used to be
    editing YAML — through this app's file editor, or in a shell. Text is
    exactly what a form cannot validate before it is written, and what nobody
    can validate at all once it is.

    Returns the config, whether it currently validates, and the schema, so a
    client builds its fields from what the pipeline accepts rather than from a
    list that can drift away from it.
    """
    proj = _project_dir(session, user, project_id)
    path = _board_config_path(proj, project_id, board)
    raw: dict = {}
    valid = True
    errors: list[str] = []
    if path.is_file():
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            # Not valid YAML at all. Report it rather than 500: this endpoint
            # exists precisely so a broken file can be repaired through a form.
            return {"board": board, "exists": True, "valid": False,
                    "errors": [f"not valid YAML: {exc}"], "config": {},
                    "schema": _project_config_schema()}
        errors = board_config.errors(raw)
        valid = not errors
    return {"board": board, "exists": path.is_file(), "valid": valid,
            "errors": errors, "config": raw, "schema": _project_config_schema()}


@app.put("/api/projects/{project_id}/boards/{board}/config")
def put_board_config(
    project_id: str,
    board: str,
    body: BoardConfigBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Write one board's config, or refuse and say why.

    Validated here as well as on load, and the duplication is deliberate: this
    stops a bad value reaching the file, and Stage 5's check stops one that
    arrived some other way. Neither makes the other redundant — a form cannot
    police a hand-edit or a git merge, and a load-time check cannot tell you
    about your typo while you are still looking at the field.

    Written through a temporary file in the same directory and replaced
    atomically, so an interrupted write cannot leave a half-parsed config where
    a whole one used to be.
    """
    proj = _project_dir(session, user, project_id)
    path = _board_config_path(proj, project_id, board)
    # Schema and geometry both, as Stage 5 checks them: a config that only
    # passed the schema could still name a self-crossing outline or
    # dimensions that disagree with it, and stop the next build.
    errors = board_config.errors(body.config)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(board_config.render(body.config), encoding="utf-8")
    os.replace(tmp, path)
    workspaces.touch(project_id, user.id)
    return {"ok": True, "board": board}


def _board_config_path(proj: Path, project_id: str, board: str) -> Path:
    """Where this board's project.yaml lives.

    A board's config sits beside that board's markdown: two boards do not share
    an outline or a stackup, and writing both to the project root would let the
    second silently describe the first.
    """
    try:
        man = project_manifest.discover(proj, project_id=project_id)
        return project_manifest.board_dir(proj, man, board) / "project.yaml"
    except (project_manifest.ManifestError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _project_config_schema() -> dict:
    return hdm_schema._load("project_config")


# Directories that are noise in a tree rather than content: history, machinery,
# and anything a package manager owns.
# .blpl is the app's own state — conversations, proposals, the usage ledger.
# It is project data in the sense that it is committed with the project, but it
# is not project *content*: every file in it already has a screen that renders
# it properly, and offering the raw JSONL in a file tree invites opening a
# 120 kB transcript in a text editor instead of the chat panel.
_TREE_SKIP = {".git", ".history", ".blpl", "node_modules", "__pycache__", ".venv"}

# What a file is *for*, which is what a reader actually wants to sort by. The
# extension alone does not say it — a .md under .notes/ is correspondence, a .md
# beside it is design intent, and the pipeline treats them very differently.
def _tree_role(rel: Path) -> str:
    parts = rel.parts
    if ".notes" in parts:
        return "note"
    if parts and parts[0] == ".pipeline":
        return "artifact"
    if parts and parts[0] == "datasheets":
        return "datasheet"
    if parts and parts[0] == quarantine.REFERENCES_DIRNAME:
        return "reference"
    # vis/ is where a project's diagrams and renders live. It is design work —
    # the architecture drawing is usually the first thing anybody makes and the
    # thing they come back to — so it belongs in the group that opens by
    # default, not filed under "other" with the scratch files.
    if parts and parts[0] in {"vis", "diagrams"}:
        return "design"
    if parts and parts[0] == quarantine.QUARANTINE_DIRNAME:
        # Shown, not hidden. Hiding retrieved files would mean the only place
        # anything untrusted lives is also the only place nobody looks.
        return "quarantined"
    if rel.suffix.lower() in {".mmd", ".mermaid"}:
        return "diagram"
    if rel.suffix.lower() in {".md", ".markdown"}:
        return "design"
    if rel.suffix.lower() in {".kicad_pcb", ".kicad_sch", ".kicad_pro"}:
        return "kicad"
    return "other"


_TREE_MAX_DEPTH = 6


def _walk_tree(root: Path, base: Path, depth: int = 0) -> list[dict]:
    """Everything in a project, as a flat list of nodes with relative paths.

    Flat rather than nested: the client groups it, and a flat list is far easier
    to filter, sort and diff than a tree of dicts. Depth-limited because a
    runaway directory should cost a truncated listing rather than a hung request.
    """
    return _walk_dir(root, base, depth)[0]


def _walk_dir(root: Path, base: Path, depth: int) -> tuple[list[dict], bool]:
    """The nodes under ``root``, and whether they are *all* of what is there.

    A folder whose listing left something out is marked ``incomplete``, because
    the tree is also what decides whether deleting the folder can be undone
    (Codex, PR #17). The delete is an rmtree: it removes what the listing
    skipped — a ``node_modules`` or a nested ``.git`` — and what lay below the
    depth limit, and the client, seeing no untracked file among what was
    listed, promised all of it back from history. Unlisted is not tracked.
    """
    if depth > _TREE_MAX_DEPTH:
        # Past the limit nothing is listed. Complete only if nothing is there.
        try:
            return [], next(root.iterdir(), None) is None
        except OSError:
            return [], False
    out: list[dict] = []
    complete = True
    try:
        entries = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return out, False
    for p in entries:
        if p.name in _TREE_SKIP:
            # Never a delete target itself — the delete route refuses any
            # dot-segment, and the rest are tooling, not content — but a
            # folder holding one loses it to the rmtree all the same.
            complete = False
            continue
        rel = p.relative_to(base)
        if p.is_dir():
            node = {"path": str(rel), "name": p.name, "dir": True, "role": _tree_role(rel)}
            out.append(node)
            below, whole = _walk_dir(p, base, depth + 1)
            if not whole:
                node["incomplete"] = True
                complete = False
            out.extend(below)
            continue
        try:
            st = p.stat()
        except OSError:
            complete = False
            continue
        out.append(
            {
                "path": str(rel),
                "name": p.name,
                "dir": False,
                "role": _tree_role(rel),
                "bytes": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return out, complete


@app.get("/api/projects/{project_id}/tree")
def get_tree(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Every file in the project, whatever produced it.

    The artifact list only ever covered ``.pipeline/``, so anything written
    anywhere else was invisible — a datasheet the assistant fetched sat on disk
    with no screen in the app that would show it. A project is more than its
    pipeline output, and the tree is the only view that says so.
    """
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    if workspace_files.enabled():
        return workspace_files.tree(project_id, proj)
    nodes = _walk_tree(proj, proj)
    # Marked on the node so the tree can say which files the assistant has been
    # told to leave alone, rather than that living only in a JSON file.
    ignored = llm_ignore.paths(proj)
    recoverable = _recoverable_paths(proj)
    for node in nodes:
        if not node["dir"] and node["path"] in ignored:
            node["llm_ignore"] = True
        # Marked so a delete can say whether git can bring the file back: an
        # untracked or ignored file deleted here is gone, and a confirmation
        # promising "one revert away" would be untrue for it. So is a tracked
        # file with uncommitted changes — the delete commits only the deletion,
        # so a revert restores HEAD's bytes, not the ones that were deleted.
        # Still named "untracked" for the client, which asks only "can git
        # bring this back as it is?".
        if not node["dir"] and recoverable is not None and node["path"] not in recoverable:
            node["untracked"] = True
    # False when git could not answer; the client then promises no recovery.
    return {"project_id": project_id, "nodes": nodes, "git": recoverable is not None}


def _recoverable_paths(checkout: Path) -> set[str] | None:
    """Every path git could restore exactly as it is now, or None when git cannot say.

    That is a file in HEAD with no change since, staged or not. ``ls-files``
    alone was the earlier answer and said yes too often (Codex, PR #17): it
    lists a tracked file with uncommitted edits, and a file only ever staged,
    and deleting either loses bytes no commit holds.
    """
    def _names(*args: str) -> set[str]:
        out = subprocess.run(
            ["git", "-C", str(checkout), *args],
            capture_output=True, timeout=20, check=True,
        ).stdout
        return {p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p}

    try:
        committed = _names("ls-tree", "-r", "-z", "--name-only", "HEAD")
        changed = _names("diff", "--name-only", "-z", "--no-renames", "HEAD", "--")
    except (OSError, subprocess.SubprocessError):
        return None
    return committed - changed


def _tree_target(proj: Path, rel: str) -> Path:
    """Resolve a client-supplied project-relative path, or refuse it.

    The path comes from the browser, so it is untrusted: resolve first, then
    confirm the result is still inside the project. Checking the string for
    ".." instead would miss a symlink pointing out of the tree.
    """
    target = (proj / rel).resolve()
    try:
        target.relative_to(proj.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="path is outside the project")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"{rel} is not a file in this project")
    return target


# ---------------------------------------------------------------------------
# Lifecycle table
# ---------------------------------------------------------------------------

class LifecycleEdit(BaseModel):
    """One person's findings about one part."""

    mpn: str
    user_status: str | None = None
    checked_on: str | None = None
    reference: str | None = None
    acknowledged: str | None = None
    notes: str | None = None


def _lifecycle_module():
    """kicad-happy's table reader, which owns the file's shape.

    Imported rather than reimplemented. The table is written by the audit and
    read here, and two parsers for one format drift apart the first time a
    column is added. Located through find_kicad_happy() for the same reason:
    the repo already has one answer to "where is kicad-happy", and the layout
    differs between this checkout and the backend image.
    """
    import importlib
    import sys as _sys

    from blpl.agent.kicad_happy import KicadHappyMissing, find_kicad_happy

    base = find_kicad_happy()
    if base is None:
        raise HTTPException(503, str(KicadHappyMissing(
            "kicad-happy not found; cannot read the lifecycle table")))
    scripts = str(base / "skills" / "kicad" / "scripts")
    if scripts not in _sys.path:
        _sys.path.insert(0, scripts)
    return importlib.import_module("lifecycle_table")


def _lifecycle_path(proj: Path) -> Path:
    return proj / "lifecycle.md"


@app.get("/api/projects/{project_id}/lifecycle")
def get_lifecycle(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """The lifecycle table as rows, for the pane that edits it."""
    proj = _project_dir(session, user, project_id)
    path = _lifecycle_path(proj)
    if not path.is_file():
        return {"exists": False, "rows": [], "columns": [], "user_columns": []}
    mod = _lifecycle_module()
    rows = mod.read_table(str(path))
    return {
        "exists": True,
        "columns": mod.COLUMNS,
        "user_columns": mod.USER_COLUMNS,
        # departed is computed here rather than left to the client to sniff
        # out of the Notes cell: it decides whether a row is work or history,
        # and one parser for that is enough.
        "rows": [dict(r, MPN=m, departed=mod.is_departed(r))
                 for m, r in sorted(rows.items())],
    }


@app.post("/api/projects/{project_id}/lifecycle")
def post_lifecycle(
    project_id: str,
    body: LifecycleEdit,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Write one row's human columns, leaving the script's alone.

    Only the five columns a person owns are writable here, and only for a row
    that already exists. The audit owns which parts are in the table and what
    the machine thinks of them; this endpoint exists so that recording what
    *you* found does not mean hand-editing a monospace markdown table.
    """
    proj = _project_dir(session, user, project_id)
    path = _lifecycle_path(proj)
    if not path.is_file():
        raise HTTPException(404, "no lifecycle table — run the audit first")

    mod = _lifecycle_module()
    rows = mod.read_table(str(path))
    row = rows.get(body.mpn)
    if row is None:
        raise HTTPException(404, f"{body.mpn} is not in the lifecycle table")

    for field, column in (
        ("user_status", "User Status"),
        ("checked_on", "Checked On"),
        ("reference", "Reference"),
        ("acknowledged", "Acknowledged"),
        ("notes", "Notes"),
    ):
        value = getattr(body, field)
        if value is not None:
            row[column] = value.strip()

    # Acknowledging clears the flag immediately, rather than leaving it stale
    # until the next audit. The column is the work list; it has to be right
    # the moment someone has done the work.
    if (row.get("Acknowledged") or "").strip():
        row["Ack?"] = ""

    path.write_text(mod.render_table(list(rows.values())), encoding="utf-8")
    return {"ok": True, "mpn": body.mpn, "row": row}


@app.get("/api/projects/{project_id}/blob")
def get_blob(
    project_id: str,
    path: str,
    request: Request,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> Response:
    """Serve any one file from the project, by its tree path.

    A PDF — by its bytes, not its name — is handed to the browser's viewer only
    if those exact bytes have a clean verdict from this backend's own
    inspection (see app/pdf_verdicts.py). A viewer is precisely the thing an
    `/OpenAction` is written to talk to, and the directory a PDF sits in proves
    nothing about it: a pull, an import or a pipeline stage can put one
    anywhere. Any other PDF is an opaque download, and the file pane offers to
    inspect it.

    The bytes are read once, hashed, and that buffer is what is sent. Hashing
    the file and then handing the path to `FileResponse` would reopen it, and a
    file swapped in between would go out under the verdict of the one it
    replaced.

    Anything under ``retrieved/`` is an opaque download whatever its verdict,
    as before: it is the evidence directory, and opening a file there should be
    a deliberate act on something the user has been told is unverified.
    """
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())

    def verdict_for(digest: str):
        project = _project_or_404(session, user, project_id)
        return pdf_verdicts.lookup(session, project.id, digest, proj, ledger=_ledger_reader(proj))

    if workspace_files.enabled():
        return workspace_files.blob(proj, path, request, verdict_for)
    target = _tree_target(proj, path)
    if _is_quarantined(proj, target):
        return FileResponse(
            target,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": _disposition("attachment", target.name),
                # Without this the browser is free to sniff the bytes, decide it
                # is a PDF after all, and render it — undoing the whole point.
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": _INERT_CSP,
                "X-BLPL-Quarantined": "1",
            },
        )
    fh, size, head = _open_once(target)
    try:
        if not quarantine.looks_like_pdf(head):
            media_type, inline = _blob_disposition(target)
            # Streamed from the descriptor that was classified. Not a PDF when
            # it was looked at, and the type sent is chosen by name from a list
            # with no PDF in it, so bytes rewritten mid-stream still cannot
            # reach the viewer.
            return _file_response(request, fh, size, media_type, {
                # Always: without it the browser may sniff a text file into HTML.
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": _disposition(
                    "inline" if inline else "attachment", target.name
                ),
                # Nothing served from here is a page. Should a browser ever
                # render one as a document anyway, it runs no script and loads
                # nothing.
                "Content-Security-Policy": _INERT_CSP,
            })
    except BaseException:
        fh.close()
        raise

    return _serve_pdf(request, fh, size, UPLOAD_MAX_BYTES, target.name, verdict_for)


def _serve_pdf(request: Request, fh, size: int, limit: int, name: str, verdict_for) -> Response:
    """Judge an open PDF on its whole content, then send it (or a range of it).

    Takes ownership of ``fh``. Never inline unless every byte was read into
    the spool, the count matches what fstat said, and that content's hash has
    a clean verdict:

    - over ``limit`` by fstat, or found to be over it while spooling (a file
      someone is still appending to): too large, streamed as an attachment
      from the descriptor with no verdict. The spool stops at ``limit + 1``
      bytes, so a growing file cannot fill the temporary disk;
    - a byte count that differs from fstat: the file changed while it was
      read, so the spool (what was read) goes out as an attachment.
    """
    opaque = "application/octet-stream"
    try:
        if size > limit:
            return _file_response(request, fh, size, opaque, _opaque_headers(name, "too_large"))
        spool, digest, total = _spool_and_hash(fh, limit)
        if total > limit:
            spool.close()
            fh.seek(0)
            now = os.fstat(fh.fileno()).st_size
            return _file_response(request, fh, now, opaque, _opaque_headers(name, "too_large"))
    except BaseException:
        fh.close()
        raise
    fh.close()
    if total != size:
        return _file_response(
            request, spool, total, opaque, _opaque_headers(name, "changed"), etag=f'"{digest}"'
        )
    try:
        verdict = verdict_for(digest)
    except BaseException:
        spool.close()
        raise
    return _pdf_response(request, spool, total, digest, name, verdict)


def _pdf_response(request: Request, spool, total: int, digest: str, name: str, verdict) -> Response:
    """Send exactly the bytes that were hashed, inline only on a clean verdict.

    The verdict is decided on the whole spool before this is called, so a
    range is only ever a slice of bytes already judged, served under the
    whole file's disposition. The hash is the ETag, which is what If-Range
    compares against.
    """
    if pdf_verdicts.renders_inline(verdict):
        return _file_response(request, spool, total, "application/pdf", {
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": _disposition("inline", name),
            "X-BLPL-PDF-Verdict": verdict.verdict,
        }, etag=f'"{digest}"')
    return _file_response(
        request, spool, total, "application/octet-stream",
        _opaque_headers(name, verdict.verdict if verdict else "none"),
        etag=f'"{digest}"',
    )


# For every response that is not a clean PDF. `sandbox` with no allowances
# gives a document a unique origin with scripts, forms and plugins off, so
# even an HTML or SVG file rendered by mistake cannot reach this origin's API.
# Not sent with a clean PDF: browsers refuse to run their PDF viewer in a
# sandboxed document, and the verdict is what makes that one safe.
_INERT_CSP = "default-src 'none'; sandbox"


def _opaque_headers(name: str, verdict: str) -> dict:
    return {
        "Content-Disposition": _disposition("attachment", name),
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": _INERT_CSP,
        "X-BLPL-PDF-Verdict": verdict,
    }


def _disposition(kind: str, name: str) -> str:
    """A Content-Disposition header that a filename cannot break.

    The name is the file's name in the tree, which a pull or an upload chose. A
    quote or a newline in it written straight into ``filename="..."`` ends the
    header early or starts a new one, so anything outside plain printable ASCII
    goes as RFC 5987 ``filename*`` instead.
    """
    from urllib.parse import quote

    if all(32 <= ord(c) < 127 for c in name) and '"' not in name and "\\" not in name:
        return f'{kind}; filename="{name}"'
    return f"{kind}; filename*=utf-8''{quote(name, safe='')}"


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# How much of a file is read at a time, and how much of a PDF being judged is
# held in memory before its copy moves to disk.
_CHUNK = 256 * 1024
_SPOOL_IN_MEMORY = 8 * 1024 * 1024


def _open_once(target: Path):
    """An open file, its size, and its first kilobyte, positioned at the start.

    One descriptor, opened without following a link and checked to be a
    regular file on that descriptor. Everything after this reads from it,
    never from the path again. Nothing is read beyond the first kilobyte here:
    whether the rest has to be hashed depends on what that kilobyte is.
    """
    import stat

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(target, flags)
    except OSError:
        raise HTTPException(status_code=404, detail=f"{target.name} is not a file in this project")
    fh = os.fdopen(fd, "rb")
    try:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise HTTPException(status_code=404, detail=f"{target.name} is not a file in this project")
        head = fh.read(1024)
        fh.seek(0)
    except BaseException:
        fh.close()
        raise
    return fh, info.st_size, head


def _spool_and_hash(fh, limit: int):
    """Copy the open file to a private spool, hashing as it goes.

    The spool, not the file, is what gets sent: a file rewritten in place
    after it was hashed would otherwise go out under the verdict of the bytes
    it replaced. Small files stay in memory; larger ones move to a temporary
    file of the backend's own, so a 100 MB PDF costs disk, not RAM. Returns the
    spool (rewound), the hex digest and the byte count.
    """
    import hashlib
    import tempfile

    spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY)
    digest = hashlib.sha256()
    total = 0
    try:
        # At most limit + 1 bytes: one past is enough to know it is too large,
        # and a file still being appended to is not followed to its end.
        while total <= limit and (chunk := fh.read(min(_CHUNK, limit + 1 - total))):
            digest.update(chunk)
            spool.write(chunk)
            total += len(chunk)
        spool.seek(0)
    except BaseException:
        spool.close()
        raise
    return spool, digest.hexdigest(), total


def _hash_open_file(fh, limit: int) -> tuple[str, int]:
    """The hash and byte count of at most ``limit + 1`` bytes; see _spool_and_hash."""
    import hashlib

    digest = hashlib.sha256()
    total = 0
    while total <= limit and (chunk := fh.read(min(_CHUNK, limit + 1 - total))):
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


_UNSATISFIABLE = "unsatisfiable"


def _parse_range(value: str | None, size: int):
    """One byte range from a Range header: (first, last), None, or unsatisfiable.

    None means "send the whole file", which is a correct answer to any Range
    request, so it covers everything this does not serve: a malformed header,
    a unit other than bytes, and several ranges at once (multipart/byteranges
    is not implemented; viewers and download managers ask for one range).
    """
    if not value:
        return None
    unit, _, spec = value.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first_s, dash, last_s = spec.strip().partition("-")
    if not dash:
        return None
    first_s, last_s = first_s.strip(), last_s.strip()
    if not (first_s.isdigit() or first_s == "") or not (last_s.isdigit() or last_s == ""):
        return None
    if first_s == "":
        if last_s == "":
            return None
        n = int(last_s)
        if n == 0 or size == 0:
            return _UNSATISFIABLE
        return max(0, size - n), size - 1
    first = int(first_s)
    if last_s and int(last_s) < first:
        return None
    if first >= size:
        return _UNSATISFIABLE
    last = int(last_s) if last_s else size - 1
    return first, min(last, size - 1)


def _file_response(
    request: Request, fh, size: int, media_type: str, headers: dict, *, etag: str | None = None
) -> Response:
    """Stream an open file, or one range of it, closing it when done or abandoned.

    Every header the caller chose (disposition, nosniff, CSP, verdict) goes on
    a 200, a 206 and a 416 alike. If-Range is honoured against the ETag; a
    response with no ETag has no validator to compare, so any If-Range sends
    the whole file, which is always safe.
    """
    headers = {**headers, "Accept-Ranges": "bytes"}
    if etag:
        headers["ETag"] = etag
    wanted = _parse_range(request.headers.get("range"), size)
    if_range = request.headers.get("if-range")
    if wanted is not None and if_range is not None and if_range.strip() != etag:
        wanted = None
    if wanted == _UNSATISFIABLE:
        fh.close()
        return Response(
            status_code=416,
            media_type=media_type,
            headers={**headers, "Content-Range": f"bytes */{size}"},
        )
    first, last = (0, size - 1) if wanted is None else wanted
    length = max(0, last - first + 1)
    try:
        if first:
            fh.seek(first)
    except BaseException:
        fh.close()
        raise

    def body():
        try:
            left = length
            while left > 0 and (chunk := fh.read(min(_CHUNK, left))):
                left -= len(chunk)
                yield chunk
        finally:
            fh.close()

    headers["Content-Length"] = str(length)
    if wanted is None:
        return StreamingResponse(body(), media_type=media_type, headers=headers)
    headers["Content-Range"] = f"bytes {first}-{last}/{size}"
    return StreamingResponse(body(), status_code=206, media_type=media_type, headers=headers)


class BlobPath(BaseModel):
    path: str


def _ledger_reader(proj: Path):
    """Where pdf_verdicts.lookup reads a held ledger row: through the agent with
    workspaces on (the backend may have no mount), from the checkout without."""
    if workspace_files.enabled():
        return lambda digest: workspace_files.ledger_record(proj, digest)
    return None


def _verdict_view(proj: Path, session: Session, user: User, project_id: str, path: str) -> dict:
    if workspace_files.enabled():
        return _workspace_verdict_view(proj, session, user, project_id, path)
    target = _tree_target(proj, path)
    fh, size, head = _open_once(target)
    with fh:
        pdf = quarantine.looks_like_pdf(head)
        # Only a PDF is hashed, in pieces; the hash is all this needs.
        digest, total = (
            _hash_open_file(fh, UPLOAD_MAX_BYTES) if pdf and size <= UPLOAD_MAX_BYTES else (None, size)
        )
    too_large = total > UPLOAD_MAX_BYTES
    # Measured one size, read another: whatever was hashed is not settled as
    # this file, so the view promises nothing (and the blob route agrees).
    changed = pdf and not too_large and total != size
    out = {
        "path": path,
        "pdf": pdf,
        "quarantined": _is_quarantined(proj, target),
        "too_large": too_large,
        "changed": changed,
        "sha256": digest if not too_large and not changed else None,
    }
    if not out["pdf"] or too_large or changed:
        return {**out, **pdf_verdicts.describe(None)}
    project = _project_or_404(session, user, project_id)
    row = pdf_verdicts.lookup(session, project.id, out["sha256"], proj)
    view = pdf_verdicts.describe(row)
    if out["quarantined"]:
        # The blob route never renders from retrieved/, so neither may this say it will.
        view["inline"] = False
    return {**out, **view}


def _workspace_verdict_view(proj: Path, session: Session, user: User, project_id: str,
                            path: str) -> dict:
    """_verdict_view, with the bytes read by the workspace's agent.

    The same answer, field for field. What is hashed is the buffer the agent
    sent, so the verdict looked up is for those bytes; a file the agent
    measured at one size and read at another promises nothing, as on the
    mounted route.
    """
    rel = workspace_files.tree_rel(path)
    head, size, data = workspace_files.pdf_bytes(proj, rel, UPLOAD_MAX_BYTES)
    pdf = quarantine.looks_like_pdf(head)
    too_large = size > UPLOAD_MAX_BYTES or (pdf and data is None)
    changed = pdf and not too_large and len(data) != size
    out = {
        "path": path,
        "pdf": pdf,
        "quarantined": rel.split("/")[0] == quarantine.QUARANTINE_DIRNAME,
        "too_large": too_large,
        "changed": changed,
        "sha256": _sha256(data) if pdf and not too_large and not changed else None,
    }
    if not pdf or too_large or changed:
        return {**out, **pdf_verdicts.describe(None)}
    project = _project_or_404(session, user, project_id)
    view = pdf_verdicts.describe(pdf_verdicts.lookup(
        session, project.id, out["sha256"], proj, ledger=_ledger_reader(proj)))
    if out["quarantined"]:
        view["inline"] = False
    return {**out, **view}


@app.get("/api/projects/{project_id}/blob/verdict")
def get_blob_verdict(
    project_id: str,
    path: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """What the blob route will do with this file, and why, before the browser asks.

    The file pane reads this first, so a PDF that will download rather than
    open says so — and says what was found, or that nothing has looked — before
    anyone clicks.
    """
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    return _verdict_view(proj, session, user, project_id, path)


@app.post("/api/projects/{project_id}/blob/inspect")
def inspect_blob(
    project_id: str,
    payload: BlobPath,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Run quarantine's checks on exactly these bytes and record the verdict.

    The same `pdf_inspect` and scanner an upload goes through, on the buffer
    read once from the file, recorded against that buffer's hash. If the file
    changes afterwards it has a new hash and no verdict, so what was inspected
    is the only thing the verdict can ever apply to. The file is not moved:
    only how the browser may receive it changes.

    With workspaces on, the agent reads the file and this backend inspects the
    buffer it sent: the verdict is still this backend's own, about exactly the
    bytes it holds, and the workspace (which a hostile file might have
    compromised) only carries them.
    """
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    if workspace_files.enabled():
        head, size, body = workspace_files.pdf_bytes(
            proj, workspace_files.tree_rel(payload.path), UPLOAD_MAX_BYTES)
    else:
        target = _tree_target(proj, payload.path)
        fh, size, head = _open_once(target)
        with fh:
            # The whole file, because pdf_inspect and the scanner take bytes. Only
            # for a PDF within the cap, on request, and read once from the same
            # descriptor; one byte past the cap catches a file that grew.
            body = None
            if quarantine.looks_like_pdf(head) and size <= UPLOAD_MAX_BYTES:
                body = fh.read(UPLOAD_MAX_BYTES + 1)
    if body is not None and len(body) > UPLOAD_MAX_BYTES:
        body = None
    if body is not None and len(body) != size:
        # A verdict recorded for bytes that were still changing would be a
        # verdict about a file that never existed on disk.
        raise HTTPException(
            status_code=409,
            detail=f"{payload.path} changed while it was being read; try again once it is written",
        )
    if not quarantine.looks_like_pdf(head):
        raise HTTPException(
            status_code=400,
            detail=f"{payload.path} is not a PDF; only PDFs are inspected before they are shown",
        )
    if body is None:
        raise HTTPException(
            status_code=413,
            detail=f"{payload.path} is over the {UPLOAD_MAX_BYTES // (1024 * 1024)} MB inspection "
            "limit, so it is only ever downloaded",
        )
    project = _project_or_404(session, user, project_id)
    assessment = quarantine.assess(body, is_pdf=True)
    pdf_verdicts.record_assessment(
        session, project.id, _sha256(body), assessment,
        source=pdf_verdicts.INSPECT, user_id=user.id,
    )
    return _verdict_view(proj, session, user, project_id, payload.path)


# Types a browser displays without running anything, chosen by name. A PDF is
# not here: it is recognised by its bytes and goes through its verdict above,
# and a file merely *named* .pdf is an attachment like any other unknown type.
_INLINE_BINARY = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Text the workbench reads. Served as text/plain whatever it is, so a browser
# shows the characters instead of interpreting them.
_INLINE_TEXT = {
    ".md", ".markdown", ".txt", ".yaml", ".yml", ".json", ".csv", ".mmd", ".mermaid",
    ".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_sym", ".kicad_mod", ".net", ".log",
}


def _blob_disposition(target: Path) -> tuple[str, bool]:
    """The media type to serve a project file as, and whether inline.

    Project files include uploads released as ``not_inspected`` and anything an
    import or the KiCad desktop wrote, so the name decides nothing about safety.
    Serving an uploaded ``.html`` or ``.svg`` inline on this origin would run
    its scripts with the viewer's session and every same-origin API behind it.
    So only an allowlist of passive types is inline, text of any kind is
    ``text/plain``, and everything else is an opaque download.
    """
    suffix = target.suffix.lower()
    if suffix in _INLINE_BINARY:
        return _INLINE_BINARY[suffix], True
    if suffix in _INLINE_TEXT:
        return "text/plain; charset=utf-8", True
    return "application/octet-stream", False


def _is_quarantined(proj: Path, target: Path) -> bool:
    try:
        return target.resolve().is_relative_to(
            quarantine.quarantine_dir(proj).resolve()
        )
    except (OSError, ValueError):
        return False


@app.get("/api/projects/{project_id}/files")
def list_files(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> list[dict]:
    """Every editable design input in the project, markdown and config.

    Recursive, because a board's design document lives in the board's directory.
    The editor treats a name it cannot find here as unopenable, so anything the
    file tree offers to edit has to appear in this list or clicking it does
    nothing.

    Names are project-relative with forward slashes — the same string
    ``_editable_file`` accepts back.
    """
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    if workspace_files.enabled():
        return workspace_files.list_files(proj)
    out = []
    for f in sorted(proj.rglob("*")):
        rel = f.relative_to(proj)
        if any(part in _TREE_SKIP or part.startswith(".") for part in rel.parts):
            continue
        if f.is_file() and f.suffix.lower() in _EDITABLE_SUFFIXES:
            out.append({"name": rel.as_posix(), "bytes": f.stat().st_size})
    return out


@app.get("/api/projects/{project_id}/files/{name:path}")
def read_file(project_id: str, name: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)):
    if workspace_files.enabled():
        return workspace_files.read_file(
            _project_dir(session, user, project_id, agent_ready=workspace_files.enabled()), _editable_rel(name), name)
    target = _editable_file(session, user, project_id, name)
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"no file {name!r}")
    return {"name": name, "content": target.read_text(encoding="utf-8", errors="replace")}



def _commit_as(
    session: Session,
    user: User,
    project: Project,
    checkout: Path,
    message: str,
    master_key: bytes | None,
) -> str | None:
    """Commit ``checkout`` as ``user``, then push it if the project says to.

    The one way a route commits on a person's behalf. Identity is resolved
    here — this member's for the project, else their account default — and a
    dirty tree with neither raises the ProjectError that says where to set
    one. A push follows only a commit that happened.
    """
    return _commit_and_push(session, user, project, checkout, message, master_key)[0]


def _commit_and_push(
    session: Session,
    user: User,
    project: Project,
    checkout: Path,
    message: str,
    master_key: bytes | None,
) -> tuple[str | None, bool]:
    """``_commit_as``, also saying whether an automatic push was scheduled —
    so a caller that would otherwise push itself (Commit + Push) does not
    push the same commit a second time. What it returns for that is the
    push's number (None when none was scheduled), which the client waits for
    in the member's push status."""
    identity = gitidentity.resolve(session, user, project)
    if workspace_files.enabled():
        # Made by the workspace's agent, with this identity on the command;
        # the numbering and scheduling below are the same either way.
        out = workspace_files.commit_checkout(checkout, message, identity)
    else:
        out = projects.commit_checkout(checkout, message, identity=identity)
    seq = None
    if out is not None:
        seq = _schedule_autopush(session, user, project, checkout, master_key)
    return out, seq


def _schedule_autopush(
    session: Session, user: User, project: Project, checkout: Path, master_key: bytes | None
) -> int | None:
    """Push in the background after a commit, when the project's policy is auto.

    Everything that needs the request — the destination, the credential row —
    is resolved here, before the request's session closes; the push itself and
    the record of how it went happen on the executor. The URL is resolved now
    and handed to the push, which refuses it if origin has changed since, so a
    credential leased for one host is never offered to another (see
    Projects._destination). Whatever goes wrong is recorded on the member's row,
    including the things that fail before any push is attempted.
    """
    if project.push_policy != "auto":
        return None
    from . import gitstore

    project_id, user_id, name = project.id, user.id, project.name

    # Numbered now, in the request's transaction, so the response can hand
    # the number to the client, which waits for the result recorded with it.
    # The number is durable exactly when the response carrying it is sent:
    # both depend on this transaction committing. If it rolls back instead
    # (the commit on disk happened anyway, so the push still runs), the
    # number goes with it and the push is renumbered below, in a transaction
    # of its own — recording it under the rolled-back number set
    # last_push_seq ahead of push_requested, the next save was handed the
    # same number, and its wait was satisfied by this old result (Codex,
    # PR #19).
    seq = autopush.request(session, project_id, user_id)
    numbered = {"seq": seq}

    def record(error: str | None) -> None:
        autopush.record_now(project_id, user_id, error, seq=numbered["seq"])

    # With workspaces behind the agent, the remote is read and the push made
    # there; push_now records under the origin lock the agent holds for it.
    via = workspace_files.enabled()
    pusher = workspace_files.AGENT_PROJECTS if via else projects
    try:
        url = (workspace_files.remote_of(checkout, "push") if via
               else projects.remote_of(name, operation="push", checkout=checkout))
        if not url:
            raise ProjectError(
                "pushing is automatic for this project, but it has no remote; "
                "add one in Project settings → Git"
            )
        cred = autopush.snapshot(gitstore.for_remote(session, user, url))
    except (ProjectError, gitstore.GitStoreError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        autopush.record(session, project_id, user_id, autopush.scrub(str(detail), []), seq=seq)
        return seq

    def job() -> None:
        # The outcome is recorded inside push_now, under the origin lock, so
        # overlapping pushes record in the order they ran.
        error = autopush.push_now(pusher, name, checkout, url, cred, master_key, record)
        if error:
            logger.warning("automatic push of %s for user %s failed: %s", name, user_id, error)

    # Started once the request's transaction has ended, not now. The outcome
    # is recorded on the member's row from another session, and for a project
    # created in this very request that row is not visible to anyone until
    # this transaction commits: a push that failed fast (no credential, a
    # refused host) looked up the member, found nobody, and its failure was
    # dropped — the one thing this must never do. On rollback too, because
    # the git commit has happened whatever the database does.
    submitted = False

    def submit(*_args) -> None:
        nonlocal submitted
        if not submitted:
            submitted = True
            AUTOPUSH.submit(job)

    def submit_after_rollback(*_args) -> None:
        if submitted:
            return
        # A number nobody else can hold: drawn and committed on its own, now
        # that the rolled-back transaction (and, on SQLite, its lock) is gone.
        # No client was told the old one — the request failed — so nothing
        # waits for it.
        try:
            numbered["seq"] = autopush.request_now(project_id, user_id)
        except Exception:  # noqa: BLE001 — the push still runs, unnumbered
            logger.exception("could not renumber the automatic push of %s", name)
            numbered["seq"] = None
        submit()

    sa_event.listen(session, "after_commit", submit, once=True)
    sa_event.listen(session, "after_rollback", submit_after_rollback, once=True)
    return seq


#: Where automatic pushes run. A module attribute so the tests can swap in an
#: executor that runs the job inline and assert on its outcome.
AUTOPUSH = autopush.EXECUTOR


def _commit_landed_write(
    session: Session,
    user: User,
    project: Project,
    checkout: Path,
    message: str,
    master_key: bytes | None = None,
) -> dict:
    """Commit a write that has already landed, and say plainly if that failed.

    Four routes write a file and then commit it, and all four used to catch the
    failure and answer `committed: false` — the same answer as "there was
    nothing to commit". So when a project's repository lost its HEAD to a
    restore race, every accepted proposal, save and upload for two days went
    uncommitted with nothing anywhere saying so. Not fatal, still: the bytes
    are on disk and refusing the write that landed would be a lie. But the
    failure travels back as a sentence, and into the log. A missing commit
    identity is one such failure, and its sentence says where to set one.
    """
    try:
        out, seq = _commit_and_push(session, user, project, checkout, message, master_key)
        # push_seq: an automatic push of this write is on its way; the client
        # watches the member's push status until that push is recorded.
        return {
            "committed": out is not None,
            **(
                {"push_seq": seq, "push_wait_s": autopush.PUSH_WAIT_SECONDS}
                if seq is not None else {}
            ),
        }
    except ProjectError as exc:
        logger.warning("commit failed in %s (%s): %s", checkout, message, exc)
        return {
            "committed": False,
            "commit_error": f"saved, but not committed to git: {exc}",
        }

@app.put("/api/projects/{project_id}/files/{name:path}")
def write_file(project_id: str, name: str, body: FileBody, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes | None = Depends(optional_master_key)) -> dict:
    """Create or overwrite an editable file. Creating is intended: a fresh project
    is empty, and this is how the first design doc gets written."""
    if workspace_files.enabled():
        checkout = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
        rel = _editable_rel(name)
        written = workspace_files.write_file(checkout, rel, body.content)
        project = _project_or_404(session, user, project_id)
        activity.record(session, project, user, activity.EDITED, rel)
        commit = _commit_landed_write(session, user, project, checkout, f"edit: {rel}", master_key)
        return {"ok": True, "name": name, "path": rel, "bytes": written, **commit}
    target = _editable_file(session, user, project_id, name)
    # The spelling the file actually landed at, as the tree lists it: empty
    # segments are dropped, so ``dir//a.md`` is written as ``dir/a.md``. The
    # client opens this rather than what it typed, or the editor holds a path
    # no tree row has and every string-compared guard misses it (Codex, PR #17).
    rel = "/".join(seg for seg in name.split("/") if seg)
    # A new board's directory may not exist yet; writing its first design
    # document is how a board comes into being.
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    activity.record(
        session, _project_or_404(session, user, project_id), user, activity.EDITED, rel
    )
    # Committed, the way an accepted proposal is. This was the one edit path
    # that wrote over the previous version and kept no record of it: an edit
    # made in the workbench survived only until the next one, and was then only
    # recoverable if some later chat commit happened to sweep it up — filed
    # under someone else's rationale, which is worse than not filed at all.
    #
    # Failure to commit is reported, never fatal. The bytes are already on disk
    # and refusing the save that landed would be a lie about what happened.
    # In the checkout the file was written to, which for a member is their own
    # worktree rather than the repository directory the name resolves to.
    commit = _commit_landed_write(
        session, user, _project_or_404(session, user, project_id),
        _project_dir(session, user, project_id, agent_ready=workspace_files.enabled()), f"edit: {rel}", master_key,
    )
    return {
        "ok": True,
        "name": name,
        "path": rel,
        "bytes": target.stat().st_size,
        **commit,
    }


# Per file, not per request: a reference design is often a zip of tens of
# megabytes, and a datasheet a few. Everything that lands is committed to the
# project and sealed with it, so this is a cost on every clone and every seal,
# not only on disk. Overridable for a deployment that knows its files.
UPLOAD_MAX_BYTES = int(os.environ.get("BLPL_UPLOAD_MAX_MB", "100")) * 1024 * 1024
UPLOAD_MAX_FILES = 20


async def _read_capped(upload: UploadFile, cap: int) -> bytes | None:
    """The upload's bytes, or None as soon as it passes ``cap``.

    Read in chunks so an oversized file is refused after ``cap`` bytes rather
    than after the whole thing has been buffered.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/api/projects/{project_id}/uploads")
async def upload_files(
    project_id: str,
    files: list[UploadFile] = File(...),
    meta: str = Form("[]"),
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Put files into a project, through quarantine.

    ``meta`` is a JSON list aligned with ``files``: ``{"kind": str, "mpn": str,
    "library": str, "llm_ignore": bool}`` per file, the kind one of
    ``quarantine.UPLOAD_KINDS``. The uploader picks the kind because only they
    know whether a PDF is a datasheet or a layout guide, and the directory it
    lands in should say which. ``library`` names the project library a symbol,
    footprint, 3D model or bundle goes into, and defaults to the project's name —
    the library ``libraries/README.md`` and existing projects use.

    Each file gets its own verdict. A held file is a result, not an error: it
    stays in ``retrieved/`` as evidence and the response says why. Only a
    malformed request fails the batch.
    """
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    if len(files) > UPLOAD_MAX_FILES:
        raise HTTPException(status_code=400, detail=f"{len(files)} files; at most {UPLOAD_MAX_FILES} per upload")
    try:
        specs = json.loads(meta or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"meta is not JSON: {exc}")
    if not isinstance(specs, list) or len(specs) != len(files):
        raise HTTPException(status_code=400, detail="meta must be a list with one entry per file")
    for spec in specs:
        if not isinstance(spec, dict) or spec.get("kind") not in quarantine.UPLOAD_KINDS:
            raise HTTPException(
                status_code=400,
                detail=f"each file needs a kind: one of {sorted(quarantine.UPLOAD_KINDS)}",
            )
        if spec["kind"] in quarantine.LIBRARY_KINDS:
            # Checked before any file is read, so a typo in the library name
            # fails the request rather than half of it. Against the project's
            # own libraries, so with workspaces on the agent checks it there.
            name = str(spec.get("library") or project_id)
            if workspace_files.enabled():
                spec["library"] = workspace_files.check_library(proj, name, project_id)
            else:
                try:
                    spec["library"] = library_upload.check_library(name, proj, suggest=project_id)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))

    who = user.email or f"user {user.id}"
    project_row = _project_or_404(session, user, project_id)
    if any(spec["kind"] in quarantine.LIBRARY_KINDS for spec in specs):
        # A library upload merges into, or relinks, the libraries/ a running
        # stage is reading, which would build from two library states at
        # once. Documents land where no stage reads them mid-run, so they
        # are not held up by this.
        _refuse_while_running(session, user, project_row, "add to its libraries")
    results: list[dict] = []
    for upload, spec in zip(files, specs):
        name = upload.filename or "upload"
        data = await _read_capped(upload, UPLOAD_MAX_BYTES)
        if data is None:
            results.append({
                "name": name, "state": "rejected", "path": None,
                "reasons": [f"larger than the {UPLOAD_MAX_BYTES // (1024 * 1024)} MB upload limit"],
            })
            continue
        if workspace_files.enabled():
            res = await workspace_files.accept_upload(
                proj, data, original_name=name, kind=spec["kind"], uploaded_by=who,
                mpn=str(spec.get("mpn") or "").strip(), llm_ignore=bool(spec.get("llm_ignore")),
                library=str(spec.get("library") or ""),
            )
            if quarantine.looks_like_pdf(data):
                # The workspace inspected these bytes, and its ledger row says
                # what it found, but a verdict the browser renders by comes only
                # from an inspection this backend ran itself: a workspace is
                # exactly what a hostile file might have compromised. The bytes
                # are already here, so they are inspected here too, as "Inspect
                # now" does — otherwise every PDF uploaded to a workspace would
                # download until someone asked.
                pdf_verdicts.record_assessment(
                    session, project_row.id, _sha256(data),
                    quarantine.assess(data, is_pdf=True),
                    source=pdf_verdicts.UPLOAD, user_id=user.id,
                )
            if res.get("members"):
                # The same for each PDF inside a vendor bundle the workspace
                # unpacked: the zip's bytes are here, so its entries are read
                # out of them and inspected here, one by one, as the mounted
                # route records each entry `accept` inspected.
                _record_bundle_pdf_verdicts(session, project_row.id, data, user.id)
            results.append({"name": name, **res})
            continue
        def record_member(member: quarantine.Record) -> None:
            # Every PDF `accept` inspects during this upload — a bundle's
            # entries go through it one by one, each a file the blob route
            # may be asked to render.
            if (member.inspection or {}).get("state") != quarantine.NOT_INSPECTED:
                pdf_verdicts.record_ledger_row(
                    session, project_row.id, member.to_dict(),
                    source=pdf_verdicts.UPLOAD, user_id=user.id,
                )

        try:
            with quarantine.recording_verdicts(record_member):
                rec = quarantine.accept_upload(
                    data,
                    proj,
                    original_name=name,
                    kind=spec["kind"],
                    uploaded_by=who,
                    mpn=str(spec.get("mpn") or "").strip(),
                    llm_ignore=bool(spec.get("llm_ignore")),
                    library=str(spec.get("library") or ""),
                )
        except quarantine.LinkRefused as exc:
            # A link where quarantine would write: nothing was written.
            results.append({"name": name, "state": "rejected", "path": None,
                            "reasons": [str(exc)]})
            continue
        if quarantine.looks_like_pdf(data):
            # This backend just inspected these bytes, so its finding is the
            # verdict the blob route renders by, held or not.
            pdf_verdicts.record_ledger_row(
                session, project_row.id, rec.to_dict(),
                source=pdf_verdicts.UPLOAD, user_id=user.id,
            )
        released = rec.state == quarantine.RELEASED
        # A bundle is not itself a file in the project; its entries are.
        path = f"{rec.released_to}/{rec.released_as}" if released and rec.released_as else None
        placed = sorted({
            *(rec.placed or ([path] if path else [])),
            *(p for m in rec.members for p in m.get("placed") or []),
        }) if released else []
        if rec.llm_ignore:
            for p in placed:
                llm_ignore.set_ignored(proj, p, True, by=who)
        results.append({
            "name": name,
            "state": rec.state,
            "kind": rec.kind,
            "path": path,
            "placed": placed,
            "library": rec.library,
            "note": (rec.inspection or {}).get("placement", ""),
            "members": rec.members,
            "reasons": rec.reasons,
            "inspection": (rec.inspection or {}).get("state", ""),
            "scan": (rec.scan or {}).get("state", ""),
            "llm_ignore": rec.llm_ignore,
        })

    landed = [r for r in results if r["state"] != "rejected"]
    if landed:
        activity.record(
            session, _project_or_404(session, user, project_id), user, activity.UPLOADED,
            ", ".join(r["name"] for r in landed),
        )
    # Released files and the ledger; retrieved/ itself is gitignored, so a
    # held file never enters history.
    # Nothing to commit when every file was refused before quarantine; trying
    # anyway produced "saved, but not committed" beside a list saying nothing
    # was uploaded.
    commit = (
        _commit_landed_write(
            session, user, _project_or_404(session, user, project_id), proj,
            f"upload: {len(landed)} file(s)", master_key,
        )
        if landed
        else {"committed": False}
    )
    return {"results": results, **commit}


def _record_bundle_pdf_verdicts(session: Session, project_id: int, data: bytes,
                                user_id: int) -> None:
    """This backend's own verdict for every PDF entry of a vendor bundle."""
    try:
        members, _ = library_upload.unpack_bundle(data)
    except library_upload.Refused:
        return  # the workspace held the bundle whole for the same reason
    for member in members:
        if quarantine.looks_like_pdf(member.data):
            pdf_verdicts.record_assessment(
                session, project_id, _sha256(member.data),
                quarantine.assess(member.data, is_pdf=True),
                source=pdf_verdicts.UPLOAD, user_id=user_id,
            )


@app.get("/api/projects/{project_id}/uploads/library-names")
def upload_library_names(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """What the upload dialog needs to warn about a library name before sending.

    ``reserved`` is every stock library name, lower-cased, and the generated
    prefix; ``existing`` the project's own libraries, which stay usable whatever
    they are called. The upload itself enforces the same rule; this only lets
    the dialog say so while the name is being typed.
    """
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    existing = (workspace_files.library_names(proj) if workspace_files.enabled()
                else library_upload.existing_project_libraries(proj))
    return {
        "reserved": sorted(library_upload.stock_library_names()),
        "reserved_prefix": library_upload.GENERATED_PREFIX,
        "existing": sorted(existing),
    }


@app.get("/api/projects/{project_id}/libraries/symbols/{library}")
def list_library_symbols(
    project_id: str,
    library: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """The symbols in one project library, for choosing one to remove."""
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    if workspace_files.enabled():
        return workspace_files.library_symbols(proj, library)
    try:
        return library_upload.library_symbols(proj, library)
    except ValueError as exc:  # Refused is a ValueError
        raise HTTPException(status_code=404, detail=str(exc))


class RemoveSymbolBody(BaseModel):
    symbol: str


@app.post("/api/projects/{project_id}/libraries/symbols/{library}/remove")
def remove_library_symbol(
    project_id: str,
    library: str,
    body: RemoveSymbolBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Take one symbol out of a project library, leaving the others.

    The way out of an upload collision, and the reason it is offered per symbol:
    a flat library holds the project's own symbols beside every uploaded one,
    so deleting the file to get rid of one would take them all.
    """
    project = _project_or_404(session, user, project_id)
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    _refuse_while_running(session, user, project, "change its libraries")
    by = user.email or f"user {user.id}"
    if workspace_files.enabled():
        # Removed by the agent, beside the library, the ledger following it
        # there; a refusal comes back as the same 409.
        out = workspace_files.remove_library_symbol(proj, library, body.symbol, by)
    else:
        try:
            out = quarantine.remove_library_symbol(proj, library, body.symbol, by=by)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
    activity.record(session, project, user, activity.DELETED, f"{library}:{body.symbol}")
    commit = _commit_landed_write(
        session, user, project, proj, f"library: remove {library}:{body.symbol}", master_key
    )
    return {"ok": True, **out, **commit}


@app.get("/api/projects/{project_id}/quarantine")
def get_quarantine(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Everything that has come in from outside, newest first, and what was decided."""
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    if workspace_files.enabled():
        return workspace_files.quarantine_view(proj)
    try:
        return {"summary": quarantine.summary(proj),
                "entries": list(reversed(quarantine.ledger(proj)))[:500]}
    except quarantine.LinkRefused as exc:
        raise HTTPException(status_code=403, detail=str(exc))


class LlmIgnoreBody(BaseModel):
    path: str
    ignored: bool


@app.get("/api/projects/{project_id}/llm-ignore")
def get_llm_ignore(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    if workspace_files.enabled():
        return {"files": workspace_files.ignore_entries(proj)}
    return {"files": llm_ignore.entries(proj)}


@app.put("/api/projects/{project_id}/llm-ignore")
def set_llm_ignore(
    project_id: str,
    body: LlmIgnoreBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Put one file on the LLM-ignore list, or take it off."""
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    if workspace_files.enabled():
        r = workspace_files.ignore_set(proj, body.path, body.ignored,
                                       user.email or f"user {user.id}")
        rel, changed = r["path"], r["changed"]
    else:
        target = _tree_target(proj, body.path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"no file {body.path!r} in this project")
        rel = target.relative_to(proj.resolve()).as_posix()
        changed = llm_ignore.set_ignored(proj, rel, body.ignored, by=user.email or f"user {user.id}")
    commit: dict = {"committed": False}
    if changed:
        verb = "ignore" if body.ignored else "unignore"
        commit = _commit_landed_write(
            session, user, _project_or_404(session, user, project_id), proj,
            f"llm-{verb}: {rel}", master_key,
        )
    return {"path": rel, "ignored": body.ignored, "changed": changed, **commit}


class FolderBody(BaseModel):
    path: str


@app.post("/api/projects/{project_id}/folders")
def make_folder(
    project_id: str, body: FolderBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Create a directory in the project.

    Git does not track directories, so this leaves nothing in the history until
    something is put inside — which is fine, and is why it is worth having as
    an action rather than a side effect of naming a file with slashes in it. A
    per-MPN datasheet folder is a place you make *before* you have the file to
    put in it.
    """
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    parts = [seg for seg in body.path.replace("\\", "/").split("/") if seg]
    if (
        not parts
        or body.path.startswith("/")
        or any(seg == ".." or seg.startswith(".") for seg in parts)
    ):
        raise HTTPException(status_code=400, detail="invalid folder name")
    if workspace_files.enabled():
        workspace_files.make_folder(proj, "/".join(parts), body.path)
        return {"ok": True, "path": "/".join(parts)}
    target = proj.joinpath(*parts)
    root = proj.resolve()
    # Resolve the parent rather than the target: the target does not exist yet,
    # and a symlinked parent is the way out of the project that a string check
    # would not see.
    if not target.parent.resolve().is_relative_to(root):
        raise HTTPException(status_code=400, detail="invalid folder name")
    if target.exists():
        raise HTTPException(status_code=409, detail=f"{body.path} already exists")
    target.mkdir(parents=True)
    return {"ok": True, "path": "/".join(parts)}


def _managed_path(session: Session, user: User, project_id: str, name: str) -> tuple[Path, Path, str]:
    """Resolve a path the file manager may delete or move: (checkout, target, rel).

    Wider than ``_editable_file`` in what it may name — any file type, and
    folders, because a datasheet PDF filed in the wrong folder is exactly what
    someone wants to move — and narrower in two places:

    - nothing under a ``retrieved`` segment. Held files are kept as evidence and
      the quarantine ledger beside them never lies by omission; a delete button
      over either would let it.
    - the target itself is not resolved. A symlink inside the project is deleted
      or moved as the link, never followed, so only its *parent* is checked —
      and that parent may contain no symlink at all, since every rule here is
      decided on the lexical path.
    """
    proj = _project_dir(session, user, project_id)
    parts = [seg for seg in name.split("/") if seg]
    if (
        not parts
        or "\\" in name
        or name.startswith("/")
        or any(seg == ".." or seg.startswith(".") for seg in parts)
    ):
        raise HTTPException(status_code=400, detail=f"invalid path {name!r}")
    if "retrieved" in parts:
        raise HTTPException(
            status_code=400,
            detail=(
                "files under retrieved/ are held by quarantine and kept as "
                "evidence — they cannot be deleted or moved from here"
            ),
        )
    root = proj.resolve()
    parent = proj.joinpath(*parts[:-1]).resolve()
    if not parent.is_relative_to(root):
        raise HTTPException(status_code=400, detail=f"invalid path {name!r}")
    # Inside the project is not enough: every check here and after — the
    # retrieved/ guard above, the untrusted-folder rule, the ledger, the
    # ignore list — reads the *lexical* path. A folder link ``alias ->
    # retrieved`` would let ``alias/quarantine.json`` be deleted, and ``alias
    # -> datasheets`` would let a vendor file be moved out under a name no
    # rule recognises (Codex, PR #17). Refused rather than canonicalised: the
    # link itself can still be deleted or moved, which is all anyone needs.
    if parent != root.joinpath(*parts[:-1]):
        raise HTTPException(
            status_code=400,
            detail=f"{name!r} goes through a symlinked folder; use the folder's real path",
        )
    target = parent / parts[-1]
    if target == root:
        raise HTTPException(status_code=400, detail=f"invalid path {name!r}")
    try:
        _sandbox_for(session, user, project_id).check_write(target)
    except ReferencePolicyError as exc:
        raise HTTPException(status_code=403, detail=f"refused by the project sandbox: {exc}")
    return proj, target, "/".join(parts)


def _refuse_while_running(session: Session, user: User, project: Project, then: str) -> None:
    """409 while one of this user's runs is queued or running on the project.

    This user's runs only: their change lands in their own worktree, so someone
    else's run cannot be disturbed by it. A run reads the tree it was started
    on, and a file deleted or renamed underneath it fails the stage halfway with
    an error naming a file the user can no longer see.
    """
    in_flight = session.scalar(
        select(Run).where(
            Run.project_id == project.id,
            Run.user_id == user.id,
            Run.status.in_([runqueue.QUEUED, runqueue.RUNNING, runqueue.CANCELLING]),
        )
    )
    if in_flight is not None:
        raise HTTPException(
            status_code=409,
            detail=f"a pipeline run is in flight for this project — {then} once it finishes",
        )


@app.delete("/api/projects/{project_id}/files/{name:path}")
def delete_path(
    project_id: str, name: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes | None = Depends(optional_master_key),
) -> dict:
    """Delete a file, or a folder and everything in it.

    Committed like any other edit, so a delete is one ``git revert`` from being
    undone — which is what makes it acceptable to offer behind one confirmation.
    """
    project = _project_or_404(session, user, project_id)
    if workspace_files.enabled():
        checkout = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
        rel = workspace_files.managed_rel(name)
        _refuse_while_running(session, user, project, "delete it")
        # The agent applies this route's ledger rules beside the files: a
        # library upload's files may go, and the ledger records that they went.
        done = workspace_files.delete(checkout, rel, user.email or f"user {user.id}")
        activity.record(session, project, user, activity.DELETED, rel)
        commit = _commit_landed_write(session, user, project, checkout, f"delete: {rel}", master_key)
        return {"ok": True, "path": rel, "kind": done["kind"], **commit,
                "ignore_list_updated": done["ignore_list_updated"],
                "ledger_updated": done.get("ledger_updated", True)}
    checkout, target, rel = _managed_path(session, user, project_id, name)
    # One transaction against a concurrent upload, move or removal in this
    # checkout: the ledger rule, the delete and the ledger's record of it
    # (quarantine.locked; Codex, PR #32).
    with quarantine.locked(checkout):
        _refuse_while_running(session, user, project, "delete it")
        if not target.exists() and not target.is_symlink():
            raise HTTPException(status_code=404, detail=f"no file or folder {rel!r}")
        # The same rule as a move, for the same reason: the ledger names a released
        # file by its path and is never edited to keep up, so deleting one would
        # leave it recording a release of a file that is not there.
        # A file a library upload placed is the exception: it is the project's
        # library now, so it may go, and the ledger records that it went.
        library_files = set(quarantine.library_placements_under(checkout, rel))
        recorded = [p for p in llm_ignore.released_under(checkout, rel) if p not in library_files]
        if recorded:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{', '.join(recorded[:3])}{' and others' if len(recorded) > 3 else ''} "
                    "is recorded in the quarantine ledger as released at its current path; "
                    "deleting it would leave the ledger naming a file that is not there"
                ),
            )
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
            kind = "folder"
        else:
            target.unlink()
            kind = "file"
        ledger_updated = True
        if library_files:
            # After the delete, and caught for the same reason as the ignore list
            # below: the bytes are already gone, so failing the request skipped
            # the commit and reported a failed delete of a file that no longer
            # exists. A ledger that could not be rewritten still claims the old
            # path, which can only over-frame a later file of that name as vendor
            # content. Nothing becomes trusted. Reported instead.
            try:
                quarantine.record_library_removal(checkout, rel, by=user.email or f"user {user.id}")
            except OSError:
                ledger_updated = False
        # After the delete, unlike a move's list update, and safely so: should this
        # write fail, the file is already gone and the entry left behind can only
        # over-ignore a later file of the same name. Nothing becomes readable.
        # Caught, as a move's post-rename cleanup is (Codex, PR #17): let through,
        # the error skipped the commit and the dialog reported a failed delete of
        # bytes that were already gone. The stale entry is reported instead.
        ignore_list_updated = True
        try:
            llm_ignore.drop_entries(checkout, rel)
        except OSError:
            ignore_list_updated = False
    activity.record(session, project, user, activity.DELETED, rel)
    # Reported, never fatal, for the reason write_file gives: the change is
    # already on disk, and failing the request would misstate what happened.
    commit = _commit_landed_write(
        session, user, project, checkout, f"delete: {rel}", master_key
    )
    return {
        "ok": True, "path": rel, "kind": kind, **commit,
        "ignore_list_updated": ignore_list_updated,
        "ledger_updated": ledger_updated,
    }


class MoveBody(BaseModel):
    # Not from/to: "from" is a Python keyword, and the alias that works around
    # it warns under this FastAPI/pydantic pair.
    path: str
    to: str


@app.post("/api/projects/{project_id}/files/move")
def move_path(
    project_id: str, body: MoveBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes | None = Depends(optional_master_key),
) -> dict:
    """Rename or move a file or folder within the project.

    A plain rename followed by the usual ``add -A`` commit: git records a
    rename by content, not by how it was made, so this is what ``git mv`` would
    have produced and ``log --follow`` still finds the history.
    """
    project = _project_or_404(session, user, project_id)
    if workspace_files.enabled():
        checkout = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
        src_rel = workspace_files.managed_rel(body.path)
        dst_rel = workspace_files.managed_rel(body.to)
        _refuse_while_running(session, user, project, "move it")
        if llm_ignore.is_external(src_rel) and not llm_ignore.is_external(dst_rel):
            raise HTTPException(status_code=403, detail=(
                f"{src_rel} came from outside the project, and files there are read as "
                "untrusted wherever they sit under datasheets/ or references/. It cannot be "
                "moved out of them."))
        # The agent moves, relinks the footprints naming a moved model, and
        # has the ledger follow a library upload's files, with this route's
        # rollbacks when either cannot be done.
        done = workspace_files.move(checkout, src_rel, dst_rel, user.email or f"user {user.id}")
        activity.record(session, project, user, activity.MOVED, f"{src_rel} → {dst_rel}")
        commit = _commit_landed_write(
            session, user, project, checkout, f"move: {src_rel} -> {dst_rel}", master_key)
        return {"ok": True, "from": src_rel, "to": dst_rel, **commit,
                "relinked": done.get("relinked", []),
                "ignore_list_updated": done["ignore_list_updated"]}
    checkout, src, src_rel = _managed_path(session, user, project_id, body.path)
    _, dst, dst_rel = _managed_path(session, user, project_id, body.to)
    # One transaction, as delete_path: the move, the footprints following its
    # models and the ledger's record of it (quarantine.locked; Codex, PR #32).
    with quarantine.locked(checkout):
        _refuse_while_running(session, user, project, "move it")
        if not src.exists() and not src.is_symlink():
            raise HTTPException(status_code=404, detail=f"no file or folder {src_rel!r}")
        if dst.exists() or dst.is_symlink():
            # Never an overwrite: the file at the destination is somebody's work,
            # and a rename is not where anyone expects to find it replaced.
            raise HTTPException(status_code=409, detail=f"{dst_rel} already exists")
        if src.is_dir() and not src.is_symlink() and dst.is_relative_to(src):
            raise HTTPException(status_code=400, detail="a folder cannot be moved inside itself")
        # The assistant frames a file as untrusted by where it is: anything under
        # datasheets/ or references/ arrived from outside. Moving one out would
        # have it read back as the project's own text, vendor instructions and
        # all, so that is refused. Moving a file *in* is allowed: it only means
        # more framing, which errs the safe way.
        if llm_ignore.is_external(src_rel) and not llm_ignore.is_external(dst_rel):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{src_rel} came from outside the project, and files there are read as "
                    "untrusted wherever they sit under datasheets/ or references/. It cannot be "
                    "moved out of them."
                ),
            )
        # A released file is named in the quarantine ledger by its path, which is
        # how its origin is shown and how a datasheet is found by MPN. Moving it
        # would leave the ledger describing a file that is not there, and the
        # ledger is never edited to keep up, so a released file stays put.
        # Except a library upload's files, which the ledger follows (see
        # quarantine.record_library_move): a library is reorganised like any other.
        library_files = set(quarantine.library_placements_under(checkout, src_rel))
        recorded = [p for p in llm_ignore.released_under(checkout, src_rel) if p not in library_files]
        if recorded:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{', '.join(recorded[:3])}{' and others' if len(recorded) > 3 else ''} "
                    "is recorded in the quarantine ledger at its current path; moving it would "
                    "leave the ledger naming a file that is not there"
                ),
            )
        # The ignore list is keyed by path, and it changes in the same commit as
        # the move. Its new paths are written *before* the rename, beside the old
        # ones (Codex, PR #17): rewritten after, a failed write left an ignored
        # file at a name the list did not cover, readable by the assistant, with
        # the request reporting failure. See llm_ignore.cover_move for why not
        # rename-then-roll-back.
        try:
            before = llm_ignore.cover_move(checkout, src_rel, dst_rel)
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail=f"{src_rel} was not moved: the LLM-ignore list could not be updated ({e})",
            ) from e
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        except OSError as e:
            if before is not None:
                # Best effort. Should this fail too, the superset stays, which
                # names the file where it still is: over-ignoring, not exposure.
                try:
                    llm_ignore.restore(checkout, before)
                except OSError:
                    pass
            raise HTTPException(status_code=500, detail=f"{src_rel} was not moved: {e}") from e
        def undo_move() -> None:
            try:
                dst.rename(src)
            except OSError:
                pass
            if before is not None:
                try:
                    llm_ignore.restore(checkout, before)
                except OSError:
                    pass

        # A footprint names its 3D model by exact path, so a moved model takes the
        # footprints that name it along, in the same commit (Codex, PR #23): left
        # alone, the next board came out without the 3D body and nothing said so.
        moved_models = {
            src_rel + str(p)[len(str(dst)):]: dst_rel + str(p)[len(str(dst)):]
            for p in ([dst] if dst.is_file() else sorted(dst.rglob("*")) if dst.is_dir() else [])
            if p.is_file() and not p.is_symlink() and p.suffix.lower() in library_upload.MODEL_SUFFIXES
        }
        try:
            relinked = library_upload.follow_moved_models(checkout, moved_models)
        except (OSError, RuntimeError, ValueError) as e:  # ValueError: quarantine.LinkRefused
            undo_move()
            raise HTTPException(
                status_code=500,
                detail=f"{src_rel} was not moved: the footprints naming its 3D models could not be updated ({e})",
            ) from e
        if library_files:
            try:
                quarantine.record_library_move(checkout, src_rel, dst_rel, by=user.email or f"user {user.id}")
            except OSError as e:
                # The ledger still names the old path, and it is what frames a
                # vendor file as untrusted when the assistant reads it. Left moved,
                # the file would be read as the project's own text. So it goes back
                # where the ledger says it is, and the move is reported as failed.
                library_upload.restore_texts(checkout, relinked)
                undo_move()
                raise HTTPException(
                    status_code=500,
                    detail=f"{src_rel} was not moved: the quarantine ledger could not be updated ({e})",
                ) from e
        ignore_list_updated = True
        if before is not None:
            try:
                llm_ignore.finish_move(checkout, before)
            except OSError:
                # The move happened, and failing the request would misstate that,
                # as with a commit that fails. The superset still covers the file at
                # its new path; only a stale entry on the old name is left, said so.
                ignore_list_updated = False
    activity.record(session, project, user, activity.MOVED, f"{src_rel} → {dst_rel}")
    commit = _commit_landed_write(
        session, user, project, checkout, f"move: {src_rel} -> {dst_rel}", master_key
    )
    return {
        "ok": True, "from": src_rel, "to": dst_rel, **commit,
        "relinked": [rel for rel, _ in relinked],
        "ignore_list_updated": ignore_list_updated,
    }


@app.get("/api/projects/{project_id}/artifacts/{name}")
def read_artifact(project_id: str, name: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)):
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    if workspace_files.enabled():
        data = workspace_files.read_artifact(proj, name)
        if data is None:
            raise HTTPException(status_code=404, detail=f"no artifact {name!r}")
        text = data.decode("utf-8", errors="replace")
        if name.endswith(".json"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        return PlainTextResponse(text)
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
def design_sources(project_id: str, board: str | None = None,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> dict:
    proj = _project_dir(session, user, project_id)
    pipeline = proj / ".pipeline"
    resolved = _resolve_board(proj, board)
    sources = []
    archived = False
    for suffix in (".kicad_sch", ".kicad_pcb"):
        # Emitted boards carry their board in the stem, so a multi-board project
        # would otherwise show whichever happened to be newest — a different
        # board than the one on screen, with nothing saying so.
        f, was_archived = _latest_with_origin(
            pipeline, suffix, stem_contains=None if resolved is None else f"_{resolved}_"
        )
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


def _blpl_dir(session: Session, user: User, project_id: str) -> Path:
    return _project_dir(session, user, project_id) / ".blpl"


def _references_path(session: Session, user: User, project_id: str) -> Path:
    return _blpl_dir(session, user, project_id) / "references.json"


def _conversations_dir(session: Session, user: User, project_id: str) -> Path:
    return _blpl_dir(session, user, project_id) / "conversations"


def _load_manifest(session: Session, user: User, project_id: str) -> ReferenceManifest:
    """The project's reference manifest, or an empty one rooted at the project.

    A corrupt manifest degrades to empty rather than 500ing the whole project:
    an unreadable references.json should cost you your external references, not
    access to your board.
    """
    proj = _project_dir(session, user, project_id)
    path = _references_path(session, user, project_id)
    if path.is_file():
        try:
            return ReferenceManifest.load(path)
        except Exception:
            pass
    return ReferenceManifest.empty(project_id=project_id, workspace_root=proj)


def _sandbox_for(session: Session, user: User, project_id: str) -> FilesystemSandbox:
    return FilesystemSandbox(
        manifest=_load_manifest(session, user, project_id),
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
def get_references(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> dict:
    manifest = _load_manifest(session, user, project_id)
    return {
        "project_id": project_id,
        "workspace_root": str(manifest.workspace_root),
        "references": [r.to_dict() for r in manifest.references],
    }


@app.put("/api/projects/{project_id}/references")
def put_references(
    project_id: str, payload: ReferenceManifestInput, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)
) -> dict:
    manifest = _load_manifest(session, user, project_id)
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

    # references.json is the file that *defines* the sandbox, so this is the one
    # write in the backend that cannot be checked by the sandbox afterwards.
    # Validate before storing: the whole list is refused if any entry is bad,
    # because a partially-applied policy is worse than a rejected one.
    denylist = load_global_denylist()
    for ref in refs:
        try:
            validate_reference(
                ref,
                workspace_root=manifest.workspace_root,
                denylist=denylist,
                protected_roots=[_DATA, PROJECTS_ROOT],
            )
        except ReferencePolicyError as exc:
            raise HTTPException(status_code=400, detail=f"reference {ref.name!r}: {exc}")

    manifest.references = refs
    _conversations_dir(session, user, project_id).mkdir(parents=True, exist_ok=True)
    path = _references_path(session, user, project_id)
    manifest.save(path)
    return {"references": [r.to_dict() for r in refs], "saved_to": str(path)}


@app.get("/api/projects/{project_id}/sandbox")
def sandbox_summary(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> dict:
    return _sandbox_for(session, user, project_id).summary()


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
def list_artifacts(project_id: str, board: str | None = None,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> dict:
    """Every artifact the pipeline has written, newest first.

    Top-level only. Rotated copies under archive/ are deliberately excluded —
    listing nine historical boards alongside the current one is how the artifact
    list stops being useful. /design already falls back to the archive when it
    has to, and says so.
    """
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    # The same normalization the run routes get from _resolve_board: on a
    # single-board project the implicit board's name is legal to pass and
    # means "no qualifier" — filtering filenames by it would drop everything,
    # because single-board artifacts carry no board in their names.
    board = _resolve_board(proj, board)
    if workspace_files.enabled():
        metas = workspace_files.list_artifacts(proj)
        if board:
            metas = [a for a in metas if f".{board}." in a["name"] or f"_{board}_" in a["name"]
                     or _is_project_level(a["name"])]
        metas.sort(key=lambda a: a["created"], reverse=True)
        return {"artifacts": metas}
    pipeline = proj / ".pipeline"
    if not pipeline.is_dir():
        return {"artifacts": []}
    files = [p for p in sorted(pipeline.iterdir()) if p.is_file()]
    if board:
        # Board is a filename qualifier, so filtering is a substring test on the
        # name. Project-level artifacts (crossboard.json) carry no board and are
        # kept: they are about this board as much as any other.
        files = [
            p for p in files
            if f".{board}." in p.name or f"_{board}_" in p.name or _is_project_level(p.name)
        ]
    artifacts = [_artifact_meta(p, pipeline) for p in files]
    artifacts.sort(key=lambda a: a["created"], reverse=True)
    return {"artifacts": artifacts}


# Artifacts that describe the project rather than any one board.
_PROJECT_LEVEL_ARTIFACTS = {"crossboard.json"}


def _is_project_level(name: str) -> bool:
    return name in _PROJECT_LEVEL_ARTIFACTS


@app.get("/api/projects/{project_id}/modules")
def list_project_modules(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> dict:
    """Reusable function-set modules this project can compose.

    Both roots, project-local first, because a project that vendored a module
    pinned that version deliberately and a shared copy must not shadow it.
    """
    from blpl.core.symbol_resolution import shared_modules_root
    from blpl.importer_kicad import list_modules

    project_dir = _project_dir(session, user, project_id)
    modules = list_modules(project_dir, shared_modules_root())
    for m in modules:
        m["scope"] = "project" if Path(m["root"]) == project_dir else "shared"
    return {"modules": modules, "shared_root": str(shared_modules_root())}


class NewConversationInput(BaseModel):
    title: str = "conversation"


class MessageInput(BaseModel):
    role: str
    content: str
    metadata: dict | None = None


@app.get("/api/projects/{project_id}/chat/endpoints")
def chat_endpoints(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> dict:
    """Which endpoints can answer a chat turn here, in routed order.

    Named separately from /api/settings because the picker beside the
    conversation list is asking a narrower question — not "what exists" but
    "what could answer this" — and an endpoint with no key cannot.
    """
    cfg = llmconfig.load(session, user)
    with_keys = keystore.endpoints_with_keys(session, user)
    chain = llm_resolver.resolve_chain(cfg, with_keys, "chat")
    caps = _endpoint_capabilities(cfg)
    return {
        "endpoints": [
            {
                "name": rp.name,
                "model": rp.model,
                "kind": rp.provider,
                "capabilities": caps.get(rp.name, []),
                "default": i == 0,
            }
            for i, rp in enumerate(chain)
        ]
    }


@app.get("/api/projects/{project_id}/conversations")
def get_conversations(project_id: str, include_archived: bool = False,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> list[dict]:
    return [
        m.to_dict()
        for m in list_conversations(
            _conversations_dir(session, user, project_id),
            include_archived=include_archived,
        )
    ]


class ArchiveBody(BaseModel):
    archived: bool = True


@app.post("/api/projects/{project_id}/conversations/{filename}/archive")
def archive_conversation(
    project_id: str, filename: str, body: ArchiveBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Take a conversation off the picker, or put it back.

    Not a delete, and there is deliberately no delete: a transcript records
    what was proposed and why a part was chosen, which outlives its usefulness
    in a dropdown.
    """
    d = _conversations_dir(session, user, project_id)
    if chat_sessions.active_for(filename):
        raise HTTPException(
            status_code=409,
            detail="that conversation has a turn running — stop it first",
        )
    try:
        conversations_mod.set_archived(d, filename, body.archived)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "filename": filename, "archived": body.archived}


@app.delete("/api/projects/{project_id}/conversations/{filename}/messages/{index}")
def delete_failed_message(
    project_id: str, filename: str, index: int,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Remove a question nothing ever answered, and the error that answered it.

    The one delete there is, and narrow on purpose. A transcript records what
    was proposed and why a part was chosen, so removing an *answered* exchange
    would take out evidence — and would break the history structurally, since a
    tool call and its result have to travel together.

    What this removes never had either. It is a question that reached no model
    and produced nothing, and four copies of one accumulate in the time it
    takes to work out that retrying is not the answer.

    They are already excluded from what gets sent. This is for the transcript,
    which is otherwise left showing the same paragraph four times with an error
    under each.
    """
    d = _conversations_dir(session, user, project_id)
    if chat_sessions.active_for(filename):
        raise HTTPException(
            status_code=409,
            detail="that conversation has a turn running — stop it first",
        )
    try:
        conv = Conversation.open_existing(d, filename)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    events = conv.read_all()
    if index not in chat_mod.unanswered_messages(events):
        raise HTTPException(
            status_code=400,
            detail=(
                "only a message that produced no answer at all can be removed — "
                "this one was answered, and the reply refers to it"
            ),
        )
    # The errors that followed it go too: they are about this message and mean
    # nothing without it.
    doomed = {index}
    for j in range(index + 1, len(events)):
        role = events[j].get("role")
        if role == "error":
            doomed.add(j)
        elif role == "user":
            break
    removed = conv.drop(doomed)
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------
# Component library
# --------------------------------------------------------------------------


class _Library:
    """One user's component library, as the two operations a tool needs.

    Closed over the user id rather than handed to tools as a path: a tool that
    only has ``get`` and ``put`` cannot construct another user's library
    location, even by accident, and the per-user boundary is the entirety of
    the answer to "what about an NDA datasheet".
    """

    def __init__(self, user_id: int, identity: "gitidentity.GitIdentity | None") -> None:
        self._user_id = user_id
        # Resolved once, when the turn starts, from the project the chat is in:
        # the library is the user's, but the person filing a part into it from
        # this project is who they are in this project. None means a filing
        # attempt raises ComponentError, which the tool reports.
        self._identity = identity

    def get(self, mpn: str) -> dict | None:
        return components.extraction(_DATA, self._user_id, mpn)

    def put(self, mpn: str, payload: dict) -> None:
        components.save_extraction(_DATA, self._user_id, mpn, payload, identity=self._identity)

    def find(self, mpn: str) -> list[dict]:
        """What this user already holds for a part, fuzzily. Same closure, same
        boundary: a tool asking this cannot ask it about anyone else."""
        return components.find(_DATA, self._user_id, mpn)

    def documents(self, mpn: str) -> list[dict]:
        return components.documents(_DATA, self._user_id, mpn)

    def document(self, mpn: str, name: str) -> bytes:
        return components.document_bytes(_DATA, self._user_id, mpn, name)


class ComponentDocBody(BaseModel):
    mpn: str


@app.get("/api/components")
def list_components(
    user: User = Depends(require_onboarded),
) -> dict:
    """This user's parts. Never anyone else's — the library is per-user, and
    that scoping is the whole of the answer to "what about NDA documents"."""
    return {"parts": components.parts(_DATA, user.id)}


@app.get("/api/components/{mpn}")
def read_component(mpn: str, user: User = Depends(require_onboarded)) -> dict:
    return {
        "mpn": mpn,
        "documents": components.documents(_DATA, user.id, mpn),
        "has_extraction": components.extraction(_DATA, user.id, mpn) is not None,
    }


@app.post("/api/components/{mpn}/documents")
async def add_component_document(
    mpn: str,
    file: UploadFile = File(...),
    user: User = Depends(require_onboarded),
) -> dict:
    data = await file.read()
    if len(data) > attachments.MAX_DOCUMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{file.filename} is larger than {attachments.MAX_DOCUMENT_BYTES // (1024*1024)} MB",
        )
    try:
        return components.add_document(
            _DATA, user.id, mpn, file.filename or "document.pdf", data,
            # The library belongs to no project, so the account default.
            identity=gitidentity.account_default(user),
        )
    except components.ComponentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/projects/{project_id}/components")
def project_components(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Which parts this project references, and at which revision.

    A bill of documents beside the bill of materials: the exact datasheet
    revision the board was designed against, per part.
    """
    proj = _project_dir(session, user, project_id)
    return {"attached": sorted(components.attached(proj))}


@app.post("/api/projects/{project_id}/components")
def attach_component(
    project_id: str, body: ComponentDocBody,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes | None = Depends(optional_master_key),
) -> dict:
    proj = _project_dir(session, user, project_id)
    project = _project_or_404(session, user, project_id)
    try:
        out = components.attach(
            proj, _DATA, user.id, body.mpn,
            identity=gitidentity.resolve(session, user, project),
        )
        if out.get("added"):
            _schedule_autopush(session, user, project, proj, master_key)
        return out
    except components.ComponentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/projects/{project_id}/components/{mpn}/update")
def update_component(
    project_id: str, mpn: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes | None = Depends(optional_master_key),
) -> dict:
    proj = _project_dir(session, user, project_id)
    project = _project_or_404(session, user, project_id)
    try:
        out = components.update(proj, mpn, identity=gitidentity.resolve(session, user, project))
        if out.get("changed"):
            _schedule_autopush(session, user, project, proj, master_key)
        return out
    except components.ComponentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/projects/{project_id}/components/{mpn}")
def detach_component(
    project_id: str, mpn: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes | None = Depends(optional_master_key),
) -> dict:
    proj = _project_dir(session, user, project_id)
    project = _project_or_404(session, user, project_id)
    try:
        components.detach(proj, mpn, identity=gitidentity.resolve(session, user, project))
        _schedule_autopush(session, user, project, proj, master_key)
    except components.ComponentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.post("/api/projects/{project_id}/conversations")
def create_conversation(
    project_id: str, payload: NewConversationInput, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)
) -> dict:
    d = _conversations_dir(session, user, project_id)
    d.mkdir(parents=True, exist_ok=True)
    conv = Conversation.create(d, title=payload.title)
    return {"slug": conv.slug, "filename": conv.path.name, "started_at": conv.started_at}


@app.get("/api/projects/{project_id}/conversations/{filename}")
def read_conversation(project_id: str, filename: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> dict:
    try:
        conv = Conversation.open_existing(_conversations_dir(session, user, project_id), filename)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "slug": conv.slug,
        "filename": conv.path.name,
        "started_at": conv.started_at,
        "events": conv.read_all(),
        # A turn survives the browser that started it — it runs here, not there.
        # Without this the only way to find a live turn was to already hold its
        # id, so a refresh (or a dropped stream) left an answer streaming into
        # nothing while the UI showed a finished-looking transcript.
        "active_turn": chat_sessions.active_for(conv.path.name),
    }


@app.post("/api/projects/{project_id}/conversations/{filename}/messages")
def append_message(
    project_id: str, filename: str, payload: MessageInput, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)
) -> dict:
    try:
        conv = Conversation.open_existing(_conversations_dir(session, user, project_id), filename)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return conv.append(payload.role, payload.content, payload.metadata)


# --------------------------------------------------------------------------
# Design chat
#
# The one place the backend talks to an LLM itself rather than spawning a stage.
# A turn runs in-process (see app/chat.py for why), streams over the same SSE
# shape the run log uses, and can only change project files by proposing an edit
# the user accepts.
# --------------------------------------------------------------------------


def _chat_chain(
    session: Session, user: User, master_key: bytes, task: str = "chat",
    project_row_id: int | None = None,
) -> list[chat_mod.Endpoint]:
    """Every endpoint routed to a task, in order, keys decrypted for this request.

    The whole chain rather than its head, because a chain used only for its
    first entry is not a fallback chain. When the head cannot serve a turn — no
    tool support, overloaded, model gone — the rest are what make the route
    worth declaring.
    """
    cfg = llmconfig.load(session, user, project_row_id)
    with_keys = keystore.endpoints_with_keys(session, user)
    out: list[chat_mod.Endpoint] = []
    for rp in llm_resolver.resolve_chain(cfg, with_keys, task):
        name = rp.name or rp.provider
        out.append(
            chat_mod.Endpoint(
                name=name,
                kind=rp.provider,  # type: ignore[arg-type]
                model=rp.model,
                api_key=keystore.get(session, master_key, user, name) if rp.needs_key else None,
                base_url=rp.base_url or None,
            )
        )
    return out


def _chat_endpoint(
    session: Session, user: User, master_key: bytes, project_row_id: int | None = None
) -> chat_mod.Endpoint:
    """The endpoint *this user's* chat turn should use, key decrypted for this
    request only.

    Shares one resolver with the pipeline (llm_resolver), so "which model am I
    talking to" has the same answer in chat as in a stage. The resolver stays
    pure — it is handed the set of endpoints this user can authenticate and
    never learns whose keys they are.
    """
    cfg = llmconfig.load(session, user, project_row_id)
    try:
        primary = llm_resolver.resolve_primary(
            cfg, keystore.endpoints_with_keys(session, user), "chat"
        )
    except llm_resolver.NoUsableProvider as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    key = (
        keystore.get(session, master_key, user, primary.name or primary.provider)
        if primary.needs_key
        else None
    )
    return chat_mod.Endpoint(
        name=primary.name or primary.provider,
        kind=primary.provider,  # type: ignore[arg-type]
        model=primary.model,
        api_key=key,
        base_url=primary.base_url or None,
    )


def _cred_resolver():
    """Distributor credentials for this request.

    Still install-level, from the server's environment: a Digi-Key or Mouser
    account is the operator's relationship with a supplier, not the user's, and
    the searches it backs are read-only catalogue lookups rather than anything
    billed per user. That is a different judgement from LLM keys, which are
    metered and personal — hence the different treatment.

    If suppliers ever become per-user, this is the seam: it already returns a
    resolver object rather than reading os.environ at the call sites.
    """
    from blpl.agent.kicad_happy import CredResolver

    return CredResolver()


# Windows discovered from a server, by endpoint name. A window does not change
# while a server is up, and asking again per turn would pay a round trip to
# learn the same number.
_DISCOVERED_CONTEXT: dict[str, int] = {}


def _context_of(session: Session, user: User, master_key: bytes, endpoint_name: str) -> int:
    """This endpoint's context window: declared, discovered, or inferred.

    Discovery happens here rather than on the Endpoint itself, because this is a
    request handler where a network call is expected and can be cached. It asks
    about *one* model — the one this endpoint uses. A self-hosted server lists
    one thing and a router lists thousands, and pulling a catalogue to find a
    single row is not a way to learn a number that does not change.
    """
    declared = llmconfig.load(session, user).endpoint(endpoint_name)
    if declared is None:
        return 0
    if declared.context_tokens:
        return int(declared.context_tokens)
    if endpoint_name in _DISCOVERED_CONTEXT:
        return _DISCOVERED_CONTEXT[endpoint_name]
    if declared.kind in ("openai-compatible", "vllm") and declared.base_url:
        found, _ = limits.describe(
            declared.base_url,
            keystore.get(session, master_key, user, endpoint_name)
            if declared.needs_key
            else None,
            declared.resolved_model(),
        )
        if found:
            _DISCOVERED_CONTEXT[endpoint_name] = int(found)
            return int(found)
    return declared.context


def _task_endpoints(
    session: Session, user: User, master_key: bytes, task: str,
    project_row_id: int | None = None,
) -> list[chat_mod.Endpoint]:
    """Every endpoint routed to a task for this user, best first.

    A list rather than one. ``resolve_chain`` has always known the fallbacks —
    it drops endpoints that cannot serve the task, so what comes back is usable
    rather than merely first — but this returned only its head, and the tool
    behind it got one attempt at one model. When that model could not hold the
    extraction schema, the endpoints sitting behind it in the very same chain
    were never asked.

    Empty rather than a loose fallback: a tool that needs vision must not
    quietly run on a model that cannot see.
    """
    cfg = llmconfig.load(session, user)

    def build(rp) -> chat_mod.Endpoint:
        name = rp.name or rp.provider
        # Carry the declared cap across. Without it the limits module fell
        # straight back to inference for every endpoint, so an override nobody
        # could set was also an override nobody would have felt.
        declared = cfg.endpoint(name)
        return chat_mod.Endpoint(
            name=name,
            kind=rp.provider,  # type: ignore[arg-type]
            model=rp.model,
            api_key=(
                keystore.get(session, master_key, user, name) if rp.needs_key else None
            ),
            base_url=rp.base_url or None,
            max_output_tokens=declared.max_output_tokens if declared else None,
        )

    chain = [
        build(rp)
        for rp in llm_resolver.resolve_chain(
            cfg, keystore.endpoints_with_keys(session, user), task
        )
    ]
    if chain:
        return chain
    # Nothing is routed to the task. Before giving up, consider the model
    # already driving this conversation: if the user has put a vision-capable
    # model in the chair, refusing to read a PDF with it because a *different*
    # route is unset is pedantry, not safety.
    if task in appconfig.VISION_TASKS:
        seeing = []
        for ep in _chat_chain(session, user, master_key, project_row_id=project_row_id):
            declared = cfg.endpoint(ep.name)
            if declared is not None and declared.can_see:
                seeing.append(ep)
        return seeing
    return []


def _task_endpoint(session: Session, user: User, master_key: bytes, task: str):
    """The best endpoint routed to a task, or None. See ``_task_endpoints``."""
    chain = _task_endpoints(session, user, master_key, task)
    return chain[0] if chain else None


#: Where the desktop's watcher looks for a project to open. The path is inside
#: the desktop's mount, which is the whole projects root by default and a single
#: project's *contents* when KICAD_DESKTOP_PROJECT names one.
_KICAD_DESKTOP_ROOT = "/config/projects"


def _kicad_desktop_mount() -> tuple[Path, str]:
    """(host directory mounted into the desktop, path it appears at there).

    Two shapes, because compose mounts two different things:
    ``./data/projects/${KICAD_DESKTOP_PROJECT:-}`` is the whole root when the
    variable is unset and one project's contents when it names one. A path
    translated with the wrong assumption points at nothing, and the watcher
    would report a project that does not exist — so this is read from the same
    variable the mount is written from rather than guessed.
    """
    named = (os.environ.get("KICAD_DESKTOP_PROJECT") or "").strip()
    if named and named != "none":
        return (PROJECTS_ROOT / named, _KICAD_DESKTOP_ROOT)
    return (PROJECTS_ROOT, _KICAD_DESKTOP_ROOT)


def _kicad_desktop_path(host_path: Path) -> str | None:
    """A path as the desktop container sees it, or None if it is not mounted."""
    base, mounted_at = _kicad_desktop_mount()
    try:
        rel = host_path.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    return f"{mounted_at}/{rel.as_posix()}" if rel.parts else mounted_at


#: A desktop heartbeat older than this is a desktop that is not running.
KICAD_ACTIVE_FRESH_S = 120
#: How long an "Open KiCad" request holds its project before the desktop's
#: own lock is expected to.
KICAD_OPEN_HOLD_S = 60


def _kicad_active_file() -> Path:
    """The desktop's heartbeat: its /config, which is data/kicad-desktop here.

    Not beside the projects. A backend run without the project mount sees an
    empty tmpfs there, so a heartbeat in it read as "nothing open" and the
    sweep sealed a tree KiCad still had open (Codex, PR #33).
    """
    configured = os.environ.get("BLPL_KICAD_ACTIVE_FILE", "").strip()
    return Path(configured) if configured else _DATA / "kicad-desktop" / ".blpl-kicad-active"


def _kicad_desktop_holds(project_id: str) -> list[str]:
    """Projects the KiCad desktop has open in this project or a member's worktree of it.

    Read from the heartbeat its watcher writes beside the open request
    (``.blpl-kicad-active``: one in-container project path per line, for every
    KiCad started on a project). A stale or missing heartbeat holds nothing, so
    a stopped desktop can never keep a project unsealed.
    """
    import time

    lines: list[str] = []
    beat = _kicad_active_file()
    try:
        if time.time() - beat.stat().st_mtime <= KICAD_ACTIVE_FRESH_S:
            lines = [ln.strip() for ln in beat.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        pass
    # An open this backend asked for in the last minute holds too, from the
    # moment the request is written: the watcher polls, and until it has seen
    # the request and published a hold of its own there was a window in which
    # nothing held the project and a seal could take it (Codex, PR #33).
    request = _kicad_open_request_file()
    try:
        if time.time() - request.stat().st_mtime <= KICAD_OPEN_HOLD_S:
            lines += [ln.strip() for ln in request.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        pass
    roots = [r.rstrip("/") for r in (
        _kicad_desktop_path(PROJECTS_ROOT / project_id),
        _kicad_desktop_path(PROJECTS_ROOT / worktrees.WORKTREES_DIR / project_id),
    ) if r]
    return [ln for ln in lines if any(ln == r or ln.startswith(r + "/") for r in roots)]


def _kicad_open_request_file() -> Path:
    """The host-side file the desktop watcher polls.

    It lives at the top of whatever is mounted, so it lands at
    ``/config/projects/.blpl-kicad-open`` in the container under either shape.
    """
    base, _ = _kicad_desktop_mount()
    return base / ".blpl-kicad-open"


def _kicad_bridge_url() -> str | None:
    """Where the KiCad MCP server is, if one is configured.

    Env var first so a local `blpl serve` can point at a server started by hand
    without editing the committed config.
    """
    from_env = os.environ.get("BLPL_KICAD_MCP")
    if from_env:
        return from_env
    server = _load_config().mcp.get("kcaa")
    return server.url if server else None


@app.post("/api/projects/{project_id}/kicad/open")
def open_in_kicad_desktop(
    project_id: str,
    board: str | None = None,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Ask the KiCad desktop to open this project's current board.

    The desktop autostarts a bare ``kicad``, so opening it from the workbench
    landed on an empty install — every file mounted and readable, none of them
    opened. That reads as "the desktop is broken" and is the most common thing
    to conclude about it.

    The app cannot start a process in that container and KasmVNC takes no file
    argument, so the two sides meet on a file: this writes the project path as
    the *desktop* sees it, and a watcher there opens it. Writing the path from
    here rather than having the watcher guess is what makes it follow the board
    somebody is actually looking at.

    Answering 200 with ``opened: false`` rather than raising when there is no
    board yet: a project whose pipeline has not reached stage 6 has nothing to
    open, which is a fact about the project and not an error the caller did
    anything to cause.
    """
    proj = _project_dir(session, user, project_id)
    pipeline_dir = proj / ".pipeline"
    resolved = _resolve_board(proj, board)
    # Delimited, not a bare substring. `_latest_with_origin` does a plain `in`
    # test on the filename, so "core" also matches `core-board_sb-ant_….kicad_pro`
    # — the project name carries the board name — and the reverse sort then
    # hands back a different board's project. `_core_` cannot. The /design route
    # already does it this way.
    pro, _ = _latest_with_origin(
        pipeline_dir, ".kicad_pro", None if resolved is None else f"_{resolved}_"
    )
    if pro is None:
        return {"opened": False, "reason": "no KiCad project has been emitted yet"}

    desktop_path = _kicad_desktop_path(pro)
    if desktop_path is None:
        # Two ways to land here, and they need different words because only one
        # of them is something the reader can act on.
        #
        # A member who is not the owner works in a git worktree beside the
        # project rather than in it. With the whole root mounted that is still
        # inside the mount and translates fine; with KICAD_DESKTOP_PROJECT
        # naming one project, only the owner's checkout is mounted and the
        # worktree genuinely is not there. Opening the owner's copy instead
        # would be worse than saying so — it would show somebody else's tree
        # and quietly omit their own edits.
        try:
            in_worktree = worktrees.WORKTREES_DIR in pro.resolve().parts
        except OSError:
            in_worktree = False
        if in_worktree:
            return {
                "opened": False,
                "reason": (
                    "the desktop has a single project mounted, which is the "
                    "owner's checkout — your own worktree is not on it, so the "
                    "board you are looking at cannot be opened there"
                ),
            }
        return {
            "opened": False,
            "reason": "this project is not the one mounted in the KiCad desktop",
        }

    request = _kicad_open_request_file()
    try:
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(desktop_path + "\n", encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not ask KiCad to open: {exc}")
    return {"opened": True, "path": desktop_path, "board": resolved}


@app.post("/api/projects/{project_id}/conversations/{filename}/attachments")
async def upload_attachments(
    project_id: str,
    filename: str,
    files: list[UploadFile] = File(...),
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> list[dict]:
    """Store files for a message that has not been sent yet.

    Upload and send are separate steps on purpose. A datasheet takes a moment to
    arrive and the message it belongs to is usually still being typed; coupling
    them would mean the send button blocks on the upload, and a failed upload
    would take the typed message down with it. This way the composer shows the
    file arriving, and the send that follows carries ids.

    Content-addressed, so re-attaching a file already in the store costs one
    hash and no disk.
    """
    conv_dir = _conversations_dir(session, user, project_id)
    try:
        Conversation.open_existing(conv_dir, filename)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if len(files) > attachments.MAX_PER_MESSAGE:
        raise HTTPException(
            status_code=400,
            detail=f"{len(files)} files; at most {attachments.MAX_PER_MESSAGE} per message",
        )

    project_row = _project_or_404(session, user, project_id)
    saved: list[dict] = []
    for upload in files:
        data = await upload.read()
        try:
            # In a worker thread: saving now inspects the PDF (inflating every
            # stream) and asks the scanner, which may take its full timeout.
            # On the event loop, ten such files blocked every other request.
            att = await asyncio.to_thread(
                attachments.save, conv_dir, upload.filename or "attachment", data
            )
            if att.kind == "document" and att.assessment is not None:
                pdf_verdicts.record_assessment(
                    session, project_row.id, att.id, att.assessment,
                    source=pdf_verdicts.ATTACHMENT, user_id=user.id,
                )
            saved.append(att.to_dict())
        except attachments.AttachmentRejected as exc:
            # One bad file fails the batch rather than half-attaching. The user
            # is standing there watching; a partial result they have to
            # reconcile is worse than a clear refusal they can retry.
            raise HTTPException(status_code=400, detail=str(exc))
    return saved


@app.get("/api/projects/{project_id}/attachments/{attachment_id}")
def get_attachment(
    project_id: str, attachment_id: str,
    request: Request,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> Response:
    """Serve stored bytes back so the transcript can show what was attached.

    Behind the same project authorisation as everything else — attachments are
    project data, and a raw sha256 is not an access token.
    """
    conv_dir = _conversations_dir(session, user, project_id)
    path = attachments.path_of(conv_dir, attachment_id)
    if path is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    # The same rule as the blob route: the bytes being sent decide, read once.
    # The store only ever wrote PNG, JPEG, GIF, WebP or PDF, but it sits in the
    # checkout's .blpl/, and its file names are not proof of what is in them.
    fh, size, head = _open_once(path)
    if not quarantine.looks_like_pdf(head):
        media_type = attachments.media_type_of(path)
        if media_type not in attachments.SUPPORTED_TYPES or media_type == "application/pdf":
            media_type = "application/octet-stream"
        return _file_response(request, fh, size, media_type, {
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": _INERT_CSP,
        })
    def verdict_for(digest: str):
        project = _project_or_404(session, user, project_id)
        return pdf_verdicts.find(session, project.id, digest)

    return _serve_pdf(
        request, fh, size, attachments.MAX_DOCUMENT_BYTES, attachments.name_of(path), verdict_for
    )


class ChatInput(BaseModel):
    content: str
    # Ids from the upload route above. Names are resolved server-side rather
    # than trusted from the client, so the transcript cannot be made to claim a
    # file is something it is not.
    attachments: list[str] = []
    # Which endpoint should answer, overriding the routed order for this turn.
    # A per-turn choice rather than a stored setting: "ask the big model about
    # this one" is a decision about a question, not a change of configuration,
    # and having it silently persist is how people end up billing a frontier
    # model for a week of small talk.
    endpoint: str = ""


@app.post("/api/projects/{project_id}/conversations/{filename}/chat")
async def start_chat_turn(
    project_id: str,
    filename: str,
    payload: ChatInput,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> dict:
    """Record the user's message and start the assistant's turn.

    The message is persisted before the turn starts, so a failure mid-answer
    costs the answer and never the question.
    """
    proj = _project_dir(session, user, project_id)
    conv_dir = _conversations_dir(session, user, project_id)
    try:
        conv = Conversation.open_existing(conv_dir, filename)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    # An attachment on its own is a message — dropping a datasheet in and
    # asking nothing is a normal way to start ("look at this"). Only a message
    # with neither text nor files is empty.
    if not payload.content.strip() and not payload.attachments:
        raise HTTPException(status_code=400, detail="message is empty")

    blocks: list[dict] = []
    for att_id in payload.attachments[: attachments.MAX_PER_MESSAGE]:
        path = attachments.path_of(conv_dir, att_id)
        if path is None:
            raise HTTPException(status_code=400, detail=f"unknown attachment {att_id}")
        media_type = attachments.media_type_of(path)
        blocks.append(
            {
                "type": "image" if media_type.startswith("image/") else "document",
                "attachment": att_id,
                "media_type": media_type,
                # Not path.name — that is the sha256 the bytes are stored under.
                "name": attachments.name_of(path),
            }
        )
    # Attachments first: providers read an image better when the question about
    # it comes after, and it matches how the message was composed.
    if payload.content.strip():
        blocks.append({"type": "text", "text": payload.content})

    # Checked before the message is written, not after. The append used to come
    # first and the in-flight check second, so a retry after a dropped stream
    # persisted the question a second time and then refused — leaving the user
    # asking twice in a transcript they never got an answer in.
    live = chat_sessions.active_for(conv.path.name)
    if live:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "turn_in_flight",
                "turn_id": live,
                "message": (
                    "this conversation already has a turn in flight; "
                    "reattaching to it"
                ),
            },
        )

    _chat_project_row = _project_or_404(session, user, project_id)
    _chat_project = _chat_project_row.id
    # Resolved once per turn: who this user is in this project, for the
    # commits a tool makes on their behalf (a library filing, a KiCad snapshot).
    _chat_identity = gitidentity.resolve(session, user, _chat_project_row)
    chain = _chat_chain(session, user, master_key, project_row_id=_chat_project)
    endpoint = _chat_endpoint(session, user, master_key, _chat_project)
    if payload.endpoint:
        chosen = next((e for e in chain if e.name == payload.endpoint), None)
        if chosen is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{payload.endpoint!r} is not routed to chat, or has no key. "
                    f"Available: {[e.name for e in chain]}"
                ),
            )
        endpoint = chosen
    # The rest of the chain still backs it up, in its configured order.
    fallbacks = tuple(e for e in chain if e.name != endpoint.name)
    conv.append("user", payload.content, {"blocks": blocks})
    try:
        turn_id = chat_sessions.start(
            chat_mod.TurnRequest(
                project_id=project_id,
                project_dir=proj,
                conversation=conv,
                endpoint=endpoint,
                fallbacks=fallbacks,
                # What the chosen model can actually be told at once. Read from
                # the declared config rather than guessed from the wire name,
                # so a self-hosted model started with a 1M window is treated as
                # having one.
                context=_context_of(session, user, master_key, endpoint.name),
                sandbox=_sandbox_for(session, user, project_id),
                usage_ledger=_blpl_dir(session, user, project_id) / "llm_usage.jsonl",
                creds=_cred_resolver(),
                endpoints_for=lambda task: _task_endpoints(session, user, master_key, task),
                library=_Library(user.id, _chat_identity),
                git_identity=_chat_identity,
                record_tool_call=lambda rec: run_manager.record_tool_call(
                    project_id, rec, conversation=conv.path.name
                ),
                kicad_url=_kicad_bridge_url(),
                conversations_dir=conv_dir,
                quarantine_sink=pdf_verdicts.sink(
                    _project_or_404(session, user, project_id).id, user.id
                ),
            )
        )
    except chat_mod.TurnInFlight as exc:
        # The pre-check above catches this in every case that matters; this is
        # the narrow race where two sends land together. Same shape of answer,
        # so the client has one path to handle rather than two.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "turn_in_flight",
                "turn_id": exc.turn_id,
                "message": str(exc),
            },
        )
    except chat_mod.ProposalError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"turn_id": turn_id, "model": endpoint.model, "endpoint": endpoint.name}


@app.get("/api/projects/{project_id}/chat/{turn_id}/events")
def stream_chat_turn(
    project_id: str, turn_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)
) -> StreamingResponse:
    """Attach to a turn: replay what it has emitted, then follow it live.

    A turn that already finished replies with a single done event — its content
    is in the conversation, which the client reloads.
    """
    _project_dir(session, user, project_id)

    async def events():
        async for event in chat_sessions.stream(turn_id):
            # A heartbeat is framed as an SSE comment: it keeps the connection
            # (and every proxy on it) awake without the client needing to know
            # about a message type that carries nothing.
            if event.get("type") == "ping":
                yield ": ping\n\n"
                continue
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/projects/{project_id}/chat/{turn_id}/cancel")
def cancel_chat_turn(
    project_id: str, turn_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)
) -> dict:
    """Stop a running turn.

    The turn lives on the server, so closing the tab never stopped one — it only
    stopped watching. Until this existed the sole way out of a turn you did not
    want (a wrong question, a tool loop grinding away, an approval you would
    rather withdraw) was to wait it out, and while it ran the conversation was
    locked to it and refused the next message.

    Idempotent: a turn that already finished reports stopped=False rather than
    404ing, because "it is not running" is the state the caller wanted either
    way.
    """
    _project_dir(session, user, project_id)
    return {"stopped": chat_sessions.cancel(turn_id)}


class ApprovalDecision(BaseModel):
    approved: bool


@app.post("/api/projects/{project_id}/chat/{turn_id}/approvals/{call_id}")
def resolve_approval(
    project_id: str, turn_id: str, call_id: str, body: ApprovalDecision,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Answer a tool call that is waiting on a human.

    The turn is parked on this, not spinning: it resumes the moment this lands.
    A 404 means the question already expired or was answered — which is
    information, not an error to swallow.
    """
    _project_dir(session, user, project_id)
    if not chat_sessions.resolve_approval(turn_id, call_id, body.approved):
        raise HTTPException(
            status_code=404, detail="no pending approval with that id — it may have timed out"
        )
    return {"ok": True, "approved": body.approved}


@app.get("/api/kicad/bridge")
def kicad_bridge_status(_: User = Depends(require_onboarded)) -> dict:
    """Whether the KiCad editing bridge is usable, and if not, exactly why.

    "Not configured", "cannot reach it", and "running but too old" are three
    different problems with three different fixes, so they are reported as three
    different answers rather than one absent capability.
    """
    from .agent.kicad_bridge import probe

    url = _kicad_bridge_url()
    if not url:
        return {
            "available": False,
            "url": "",
            "error": "no KiCad MCP server configured — set BLPL_KICAD_MCP or [mcp.kcaa] url",
        }
    status, _usable = probe(url)
    return status.to_dict()


@app.get("/api/projects/{project_id}/tool-calls")
def list_tool_calls(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> list[dict]:
    """What the agents have actually done in this project, newest first."""
    _project_dir(session, user, project_id)
    return run_manager.tool_calls_for_project(project_id)


class PolicyUpdate(PolicyFields):
    """A change to an existing project's declaration.

    Same shape as at creation. Absent fields close rather than preserve, because
    a partial update that inherited the permissive half of a previous answer
    would be a way to widen sharing without saying so.
    """


@app.get("/api/projects/{project_id}/policy")
def read_policy(
    project_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """This project's library declaration, and the consent wording in force.

    Membership, not ownership: everyone working on a board should be able to see
    what it shares. Changing it is a separate question, answered below.
    """
    from . import library_policy

    project = _project_or_404(session, user, project_id)
    row = session.scalar(select(ProjectPolicy).where(ProjectPolicy.project_id == project.id))
    return {
        "contribute": row.contribute if row else library_policy.DEFAULTS["contribute"],
        "consume": row.consume if row else library_policy.DEFAULTS["consume"],
        "unique_components": (
            row.unique_components if row else library_policy.DEFAULTS["unique_components"]
        ),
        # Whether the agreement on file is to the wording below, rather than to
        # an earlier one — a project that agreed to a different sentence has not
        # agreed to this one.
        "consented": library_policy.has_consented(row),
        "consent_text": library_policy.CONSENT_TEXT,
        "declared": row is not None,
        "choices": {
            "contribute": list(library_policy.CONTRIBUTE),
            "consume": list(library_policy.CONSUME),
            "unique_components": list(library_policy.UNIQUE),
        },
    }


@app.put("/api/projects/{project_id}/policy")
def update_policy(
    project_id: str,
    body: PolicyUpdate,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Change what a project shares. Owner only.

    Deliberately narrower than reading it. Membership is enough to *see* what a
    board shares — that is information anyone working on it should have — but
    widening it is a decision about someone else's data as much as your own, and
    it belongs with whoever owns the project.
    """
    try:
        project = projectacl.require_owner(session, user, project_id)
    except projectacl.NoSuchProject:
        raise HTTPException(status_code=404, detail=f"no project {project_id!r}")
    except projectacl.NotTheOwner:
        raise HTTPException(
            status_code=403, detail="only the owner can change what this project shares"
        )
    _record_policy(session, project, body, actor_id=user.id)
    return read_policy(project_id, user=user, session=session)


@app.get("/api/projects/{project_id}/proposals")
def list_proposals(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> list[dict]:
    """Edits the assistant has proposed and nobody has ruled on yet."""
    proj = _project_dir(session, user, project_id)
    return [p.to_dict() for p in chat_mod.ProposalStore(proj).pending()]


class ProposalDecision(BaseModel):
    action: str  # accept | reject


@app.post("/api/projects/{project_id}/proposals/{proposal_id}")
def decide_proposal(
    project_id: str, proposal_id: str, body: ProposalDecision, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes | None = Depends(optional_master_key),
) -> dict:
    """Accept an edit (write it, then commit it) or reject it.

    Accepting is refused while a stage run is in flight for this project: the
    pipeline reads these very files, and changing an input underneath a running
    stage produces artifacts that match no version of the design.
    """
    proj = _project_dir(session, user, project_id)
    store = chat_mod.ProposalStore(proj)
    try:
        proposal = store.get(proposal_id)
    except chat_mod.ProposalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"no proposal {proposal_id!r}")
    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail=f"proposal is already {proposal.status}")

    if body.action == "reject":
        store.set_status(proposal, "rejected")
        return {"ok": True, "status": "rejected"}
    if body.action != "accept":
        raise HTTPException(status_code=400, detail="action must be 'accept' or 'reject'")

    project = _project_or_404(session, user, project_id)
    # This user's runs only: their edit lands in their own worktree, so someone
    # else's run cannot be disturbed by it.
    in_flight = session.scalar(
        select(Run).where(
            Run.project_id == project.id,
            Run.user_id == user.id,
            Run.status.in_([runqueue.QUEUED, runqueue.RUNNING, runqueue.CANCELLING]),
        )
    )
    if in_flight is not None:
        raise HTTPException(
            status_code=409,
            detail="a pipeline run is in flight for this project — accept the edit once it finishes",
        )

    try:
        applied, detail = chat_mod.apply_proposal(
            proposal, proj, _sandbox_for(session, user, project_id)
        )
    except chat_mod.ProposalWriteError as exc:
        # Left pending: nothing about the proposal changed, only the disk.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not applied:
        store.set_status(proposal, "stale")
        raise HTTPException(status_code=409, detail=detail)

    store.set_status(proposal, "accepted")
    # A project without working git history still gets its file. Losing the
    # commit is worth reporting, not worth refusing the edit that already landed.
    commit = _commit_landed_write(
        session, user, _project_or_404(session, user, project_id), proj,
        f"chat: {proposal.rationale or proposal.path}", master_key,
    )
    return {"ok": True, "status": "accepted", "detail": detail, **commit}


# --------------------------------------------------------------------------
# Stage execution
# --------------------------------------------------------------------------


def _stage_env(
    session: Session,
    user: User,
    master_key: bytes,
    stage_name: str,
    project_row_id: int | None = None,
) -> dict[str, str]:
    """The subprocess environment for a stage run.

    For LLM stages, resolve the *whole* fallback chain the config prefers and has
    keys for, inject every provider's key into its own SDK env var, and hand the
    ordered chain to the pipeline as HDM_LLM_CHAIN. The pipeline then tries each in
    turn, so a dead key or an unreachable provider fails over to the next instead
    of failing the stage. This is where a user's keys meet the pipeline: they are
    decrypted here, passed in the child's environment, and never written to a
    command line or a log. Deterministic stages get the base environment unchanged.

    The environment this starts from is the server's, which no longer carries any
    provider key — so a stage that gets no key from the user gets none at all,
    rather than silently falling back to the operator's.
    """
    env = dict(os.environ)
    if stage_name not in _LLM_STAGES:
        return env
    # Stage names map to task routes: everything stage0-ish shares the stage0
    # route, everything stage1-ish the stage1 route, so a cheap model can do the
    # mechanical re-read while footprint resolution gets the expensive one.
    task = "stage0" if stage_name.startswith("stage0") else "stage1"
    return _inject_llm_env(env, session, user, master_key, task, project_row_id)


def _inject_llm_env(
    env: dict[str, str],
    session: Session,
    user: User,
    master_key: bytes,
    task: str = "default",
    project_row_id: int | None = None,
) -> dict[str, str]:
    """Add the resolved LLM fallback chain and its keys to an environment.

    Each endpoint's key goes into its *own* variable, named by the chain entry
    (``BLPL_LLM_KEY__<NAME>``). One shared ANTHROPIC_API_KEY could not express
    two Anthropic endpoints on different accounts, which is exactly what the
    endpoint registry exists to allow. The provider-wide variables are still set
    from the primary so anything reading the SDK defaults keeps working.

    Keys are this user's own — there is no server-wide fallback — and are passed
    only in the child's environment, never on a command line or in a log. Raises
    NoUsableProvider if nothing routed to the task has one, which the caller
    turns into a clear 400.
    """
    # Project scope, so an override reaches the thing that actually spends a
    # token. A setting that is stored and displayed but never consulted is
    # worse than no setting: it reads as configured.
    cfg = llmconfig.load(session, user, project_row_id)
    with_keys = keystore.endpoints_with_keys(session, user)
    chain = llm_resolver.resolve_chain(cfg, with_keys, task)
    if not chain:
        llm_resolver.resolve_primary(cfg, with_keys, task)  # raise the good message

    for rp in chain:
        if not rp.needs_key:
            continue
        key = keystore.get(session, master_key, user, rp.name or rp.provider)
        if key is None:
            continue
        env[rp.key_env] = key
        # Mirror onto the SDK's own variable for the primary only — two
        # endpoints of one kind must not fight over one global.
        provider_var = _PROVIDER_ENV.get(rp.provider)
        if provider_var and rp is chain[0]:
            env[provider_var] = key

    env["HDM_LLM_CHAIN"] = json.dumps(
        [
            {
                "provider": rp.provider,
                "endpoint": rp.name,
                "model": rp.model,
                "base_url": rp.base_url,
                "key_env": rp.key_env if rp.needs_key else "",
            }
            for rp in chain
        ]
    )
    env["HDM_LLM_PROVIDER"] = chain[0].provider
    if chain[0].model:
        env["HDM_LLM_MODEL"] = chain[0].model
    return env


def _inject_llm_env_for_tasks(
    env: dict[str, str],
    session: Session,
    user: User,
    master_key: bytes,
    tasks: Sequence[str],
    project_row_id: int | None = None,
) -> dict[str, str]:
    """Inject a chain per task, for a child that will run several stages.

    ``blpl run --from stage0 --to stage8`` is one process running stages that
    may be routed differently on purpose — a cheap model for stage0's
    mechanical re-read, an expensive one for stage1's footprint resolution.
    Resolving a single ``default`` chain for the whole range made that routing
    a setting that is stored, displayed, and never consulted, which is worse
    than not offering it.

    It also failed outright in a case that should work: a project that routes
    only ``stage1`` and has nothing usable on ``default`` got a 400 for a run
    whose stage1 was perfectly well configured.

    The first task still populates ``HDM_LLM_CHAIN`` and the provider-wide
    variables, so anything that reads the chain without naming a task — the
    agent dispatcher, the review panel, an older stage — keeps working.
    """
    chains: dict[str, list[dict]] = {}
    failures: list[Exception] = []
    primary_done = False
    for task in tasks:
        try:
            scratch = _inject_llm_env(dict(env), session, user, master_key,
                                      task, project_row_id)
        except llm_resolver.NoUsableProvider as exc:
            failures.append(exc)
            continue
        # Per-endpoint key variables are named after the endpoint, so two tasks
        # sharing one write the same name and the same value — assigned, not
        # set-defaulted, so a stale key in the server's own environment cannot
        # win over the user's.
        for k, v in scratch.items():
            if k.startswith("BLPL_LLM_KEY__"):
                env[k] = v
        chains[task] = json.loads(scratch["HDM_LLM_CHAIN"])
        if not primary_done:
            # The first routable task owns the un-suffixed variables, including
            # the provider-wide SDK ones: two tasks on different providers must
            # not fight over one global.
            primary_done = True
            env["HDM_LLM_CHAIN"] = scratch["HDM_LLM_CHAIN"]
            env["HDM_LLM_PROVIDER"] = scratch["HDM_LLM_PROVIDER"]
            env.pop("HDM_LLM_MODEL", None)
            if "HDM_LLM_MODEL" in scratch:
                env["HDM_LLM_MODEL"] = scratch["HDM_LLM_MODEL"]
            for var in _PROVIDER_ENV.values():
                if var in scratch:
                    env[var] = scratch[var]
    if not chains:
        # Every task in the range is unroutable — report the first, which is
        # the one the user hits first.
        raise failures[0]
    env["HDM_LLM_CHAINS"] = json.dumps(chains)
    return env


async def _stream_run(run_id: str, logs_dir: Path):
    """Replay a run's log from the top, then follow it live.

    A tail rather than a subscription, and that is the point: the run is in
    another container, so there is no in-process queue to attach to. Any API
    process that can see the volume can serve any run's stream, which is exactly
    what having more than one API process requires.

    Completion is read from the row, not inferred from the file. A log that has
    stopped growing is indistinguishable from a stage that is thinking.
    """
    path = runqueue.log_path(logs_dir, run_id)
    offset = 0
    yield "start", {"run_id": run_id}
    announced_running = False
    waited = 0.0

    while True:
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                chunk = fh.read()
                offset = fh.tell()
            for line in chunk.splitlines():
                yield "log", {"line": line}

        with SessionFactory() as session:
            run = session.get(Run, run_id)
            if run is None:
                yield "done", {"exit_code": runqueue.INTERRUPTED, "run_id": run_id}
                return
            finished = run.status in (runqueue.DONE, runqueue.CANCELLED)
            exit_code = run.exit_code
            status = run.status

        # A queued run has no output yet, and a stream that says nothing is
        # indistinguishable from one that has broken. Say what it is waiting for
        # instead — and keep saying it, because "no worker is running" is a real
        # state an operator has to be able to see from the UI.
        if status == runqueue.QUEUED and waited > 0 and waited % 5 < 0.3:
            yield "queued", {"waiting_for": "a worker to pick this up", "seconds": int(waited)}
        if status == runqueue.RUNNING and not announced_running:
            announced_running = True
            yield "running", {"run_id": run_id}

        if finished:
            # One last read: the worker writes the final lines and only then
            # records the exit, so stopping at the status check would truncate
            # the end of every log.
            if path.exists():
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    for line in fh.read().splitlines():
                        yield "log", {"line": line}
            yield "done", {"exit_code": exit_code if exit_code is not None else runqueue.INTERRUPTED,
                           "run_id": run_id}
            return

        await asyncio.sleep(0.25)
        waited += 0.25


# How long a run's stream may go without sending anything before it sends a
# keepalive. Freerouting prints nothing for minutes at a time, and a proxy or
# tunnel with a 60-100 s idle timeout dropped the connection mid-autoroute: the
# browser reported a network error and the panel gave up on a run that was
# still going. Same interval as the chat stream's heartbeat.
RUN_HEARTBEAT_SECONDS = chat_mod.HEARTBEAT_SECONDS


async def _with_heartbeat(events, interval: float):
    """Pass ``events`` through, yielding ("ping", None) whenever it is quiet.

    The pending read is waited on, never cancelled, so a keepalive cannot cut
    a log line in half or lose one.
    """
    it = events.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(it.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield "ping", None
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield item
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


def _run_stream_response(run_id: str) -> StreamingResponse:
    """SSE over a run's log.

    The argv echoed in the start event is safe — keys ride in the environment,
    never on a command line. A client disconnect closes only this reader; the
    run keeps going in its worker, and stopping one is an explicit
    DELETE /api/runs/{id} rather than a dropped connection.
    """

    async def events():
        async for ev, payload in _with_heartbeat(
            _stream_run(run_id, _DATA / "runs"), RUN_HEARTBEAT_SECONDS
        ):
            if ev == "ping":
                # An SSE comment: keeps every proxy on the path awake, and a
                # client that does not know about it ignores it.
                yield ": ping\n\n"
                continue
            yield f"event: {ev}\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


def _start_run(
    session: Session,
    user: User,
    project_id: str,
    kind: str,
    cmd: list[str],
    env: dict[str, str],
) -> StreamingResponse:
    """Queue a run and attach the caller to its stream.

    The API does not execute it. That is the whole change: a stage now runs in a
    worker container, so more than one API process can exist without a reader
    landing on the process that cannot see the run.
    """
    project = _project_or_404(session, user, project_id)
    # Again here, not only at save: an endpoint saved before the operator
    # removed its host, or routed in by a project override, would otherwise
    # fail inside the run at its first request.
    _check_egress(egress.targets_in_run_env(env))
    key = serverkey.load(_DATA)
    if key is None:
        raise HTTPException(status_code=503, detail="no server key; cannot dispatch runs")
    try:
        run = runqueue.enqueue(session, project, user, kind, cmd, env, key)
    except runqueue.RunActive as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    session.commit()  # the worker must be able to see it
    return _run_stream_response(run.id)


@app.post("/api/projects/{project_id}/stages/{stage_name}")
async def run_stage(
    project_id: str,
    stage_name: str,
    board: str | None = None,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> StreamingResponse:
    """Run a pipeline stage, streaming its output to the browser as it happens."""
    if stage_name not in VALID_STAGES:
        raise HTTPException(status_code=400, detail=f"unknown stage {stage_name!r}")
    # A launch needs only the checkout's path and its board list, both had
    # through the agent with the agent path on; the run itself executes in a
    # run container, so a backend without the project mount can queue it
    # (Codex, PR #20).
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    try:
        env = _stage_env(
            session, user, master_key, stage_name,
            _project_or_404(session, user, project_id).id,
        )
    except llm_resolver.NoUsableProvider as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cmd = [sys.executable, "-m", "blpl.core.cli", stage_name, "--project-dir", str(proj)]
    resolved = _resolve_board(proj, board)
    if resolved is not None:
        cmd += ["--board", resolved]
    activity.record(
        session,
        _project_or_404(session, user, project_id),
        user,
        activity.RAN,
        stage_name if resolved is None else f"{stage_name} ({resolved})",
    )
    # The run record carries the board the same way the activity log does:
    # a single-stage run used to be recorded as bare "stage1", so run history
    # could not say which of seven boards it had built.
    return _start_run(
        session, user, project_id,
        stage_name if resolved is None else f"{stage_name} ({resolved})",
        cmd, env,
    )


@app.post("/api/projects/{project_id}/release")
async def build_release(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> StreamingResponse:
    """Build the package a contract fab quotes from, gate included.

    No LLM in this path: the gate reads what the analyzers already found and the
    exports are kicad-cli. A release that depended on a model being reachable
    would be a release you could not cut in a hurry.
    """
    # Nothing is read here: the run reads the project, in its run container.
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    cmd = [sys.executable, "-m", "blpl.core.release", "--project-dir", str(proj)]
    return _start_run(session, user, project_id, "release", cmd, dict(os.environ))


@app.get("/api/projects/{project_id}/release")
def latest_release(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> dict:
    """The manifest of the most recent release, or why there is none."""
    manifest = _project_dir(session, user, project_id) / "release" / "latest.json"
    if not manifest.is_file():
        return {"exists": False}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"unreadable release manifest: {exc}")
    data["exists"] = True
    return data


@app.get("/api/projects/{project_id}/preflight")
def preflight(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> dict:
    """What Stage 0 would drop or misread, without running anything.

    Served synchronously rather than through the SSE stage runner because the
    doctor reads markdown and touches no subprocess — and because the panel
    wants the findings *structured*, which a log stream cannot give it. Running
    it is free and mutates nothing, so the panel can ask on every visit.
    """
    from blpl.core import crossboard, doctor

    proj = _project_dir(session, user, project_id)
    try:
        man = project_manifest.discover(proj)
    except project_manifest.ManifestError:
        man = None

    try:
        if man is None or man.implicit:
            return doctor.run(proj).to_dict()
        # A multi-board project has two kinds of finding, and they answer
        # different questions: the doctor says what one board's markdown would
        # drop, the cross-board report says what happens where boards meet. The
        # second is the one no per-board check can reach, so it is not hidden
        # behind a board selection.
        per_board = {}
        artifacts: dict[str, dict] = {}
        for b in (x.name for x in man.boards):
            bdir = project_manifest.board_dir(proj, man, b)
            if bdir.is_dir():
                # The project, qualified by board — not the board directory as
                # a project. libraries/ and the vendored modules are scoped to
                # the project, so a doctor rooted at core/ could not find the
                # project's own symbol library and reported every hand-drawn
                # part on every sub-board as missing.
                per_board[b] = doctor.run(proj, board=b, layout=False).to_dict()
            art = project_manifest.artifact_path(
                proj, "design_artifact.deterministic", board=b
            )
            if art.is_file():
                try:
                    artifacts[b] = json.loads(art.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
        report = crossboard.check(man, artifacts) if artifacts else None
        return {
            "multi_board": True,
            # Findings about the project rather than any one board, such as a
            # folder of design tables no board declares. Once, not per board.
            "project": doctor.layout(proj).to_dict(),
            "boards": per_board,
            "crossboard": report.to_dict() if report else None,
            # Named so the panel can say why the cross-board section is empty
            # rather than implying everything passed.
            "crossboard_ready": sorted(artifacts),
            "crossboard_missing": sorted(
                b.name for b in man.boards if b.name not in artifacts
            ),
        }
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not read the project: {exc}")


@app.get("/api/projects/{project_id}/release/latest.zip")
def download_release(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> Response:
    """The package itself.

    Served whether or not the gate passed — a refused package carries a
    READ-ME-FIRST saying so, which is more useful than a download that silently
    does not exist.
    """
    archive = _project_dir(session, user, project_id) / "release" / "latest.zip"
    if not archive.is_file():
        raise HTTPException(status_code=404, detail="no release has been built for this project")
    return Response(
        content=archive.read_bytes(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{project_id}-release.zip"'},
    )


@app.post("/api/projects/{project_id}/review-panel")
async def run_review_panel(
    project_id: str,
    board: str | None = None,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
) -> StreamingResponse:
    """Review the board with every endpoint routed to the review_panel task.

    The one place the endpoint chain means "all of these" rather than "these in
    order": a panel of one is just a review, and the value comes from members
    with different blind spots disagreeing.
    """
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    # Which board, before which model: not saying which board is a malformed
    # request, while having no provider configured is a deployment that needs
    # setting up. Answering the second when the first is also true sends people
    # off configuring keys for a request that would still be rejected.
    resolved = _resolve_board(proj, board)
    try:
        env = _inject_llm_env(
            dict(os.environ), session, user, master_key, "review_panel",
            _project_or_404(session, user, project_id).id,
        )
    except llm_resolver.NoUsableProvider as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    cmd = [
        sys.executable, "-m", "blpl.agent.dispatch", "review-panel",
        "--project-dir", str(proj),
    ]
    if resolved is not None:
        cmd += ["--board", resolved]
    return _start_run(
        session, user, project_id,
        "review-panel" if resolved is None else f"review-panel ({resolved})",
        cmd, env,
    )


# The stages the whole-pipeline runner understands, in order. The CLI `run`
# command drives 0→8; this is the allowlist the UI's from/to must fall within.
_PIPELINE_STAGES = [
    "stage0", "stage1", "stage2", "stage3", "stage4",
    "stage5", "stage6", "stage6a", "stage7", "stage8",
]


@app.post("/api/projects/{project_id}/pipeline")
async def run_pipeline(
    project_id: str,
    from_stage: str = "stage0",
    to_stage: str = "stage8",
    board: str | None = None,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
    master_key: bytes = Depends(require_master_key),
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
    proj = _project_dir(session, user, project_id, agent_ready=workspace_files.enabled())
    # Every stage in the range resolves a board, so the range needs one too.
    # Without it "run the pipeline" is the one control on a multi-board project
    # that cannot work, while each stage individually can. Asked before the
    # provider lookup, for the same reason as the panel above.
    #
    # `all` is the one name that is not a board: on a multi-board project it
    # runs the range for every board in manifest order and then the cross-board
    # check, exactly as `blpl run --board all` does. A pipeline run is otherwise
    # about ONE board, and the run's label says which.
    every_board = False
    if board == "all":
        try:
            man = (workspace_files.manifest(proj) if workspace_files.enabled()
                   else project_manifest.discover(proj))
        except project_manifest.ManifestError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if man.implicit:
            raise HTTPException(
                status_code=400,
                detail=f"{proj.name} is a single-board project; there is no 'all' to run",
            )
        every_board = True
        resolved = "all"
    else:
        resolved = _resolve_board(proj, board)

    env = dict(os.environ)
    lo, hi = _PIPELINE_STAGES.index(from_stage), _PIPELINE_STAGES.index(to_stage)
    # One chain per LLM task the range will actually reach, not one `default`
    # chain for all of them: stage0 and stage1 are routable separately and a
    # range spanning both has to carry both routes.
    tasks = [t for t, stage in (("stage0", "stage0"), ("stage1", "stage1"))
             if lo <= _PIPELINE_STAGES.index(stage) <= hi]
    if tasks:
        try:
            env = _inject_llm_env_for_tasks(
                env, session, user, master_key, tasks,
                _project_or_404(session, user, project_id).id,
            )
        except llm_resolver.NoUsableProvider as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    cmd = [
        sys.executable, "-m", "blpl.core.cli", "run",
        "--project-dir", str(proj),
        "--from", from_stage, "--to", to_stage,
        "--continue-on-error",
    ]
    if resolved is not None:
        cmd += ["--board", resolved]
    label = f"pipeline {from_stage}→{to_stage}"
    if every_board:
        label += " (all boards)"
    elif resolved is not None:
        label += f" ({resolved})"
    return _start_run(session, user, project_id, label, cmd, env)


# --------------------------------------------------------------------------
# Run history — the durable record behind the streams above
# --------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/runs")
def list_runs(project_id: str, user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope)) -> list[dict]:
    """Past and live runs for a project, newest first."""
    project = _project_or_404(session, user, project_id)
    rows = session.scalars(
        select(Run).where(Run.project_id == project.id).order_by(Run.created_at.desc()).limit(50)
    )
    return [_run_json(r) for r in rows]


def _run_json(run: Run) -> dict:
    return {
        "id": run.id,
        "project": run.project.name,
        "kind": run.kind,
        "status": run.status,
        "running": run.status in (runqueue.QUEUED, runqueue.RUNNING, runqueue.CANCELLING),
        "exit_code": run.exit_code,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "by": run.user.email if run.user else "",
    }


def _visible_run(session: Session, user: User, run_id: str) -> Run:
    """A run, if its project is one this user may open.

    Runs are reached by an opaque id rather than through a project path, so the
    permission check has to be made here explicitly — the id alone must not be a
    way around project membership.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
    try:
        projectacl.require_member(session, user, run.project.name)
    except projectacl.NoSuchProject:
        # 404 naming the *run*, not the project: the caller asked about a run id
        # and telling them a project exists that they cannot see would leak the
        # thing the id was meant to hide.
        raise HTTPException(status_code=404, detail=f"no run {run_id!r}")
    return run


@app.get("/api/runs/{run_id}/stream")
def stream_run(
    run_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> StreamingResponse:
    """Attach to a run: full log replay, then the live tail if it's still going.

    This is what makes a mid-run browser refresh a non-event — reattach here
    and catch up. On a finished run it replays and ends.
    """
    _visible_run(session, user, run_id)
    return _run_stream_response(run_id)


@app.get("/api/runs/{run_id}/log")
def run_log(
    run_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> PlainTextResponse:
    _visible_run(session, user, run_id)
    path = runqueue.log_path(_DATA / "runs", run_id)
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    return PlainTextResponse(text)


@app.delete("/api/runs/{run_id}")
def stop_run(
    run_id: str,
    user: User = Depends(require_onboarded),
    session: Session = Depends(session_scope),
) -> dict:
    """Ask a run to stop.

    A request rather than a kill: the process is in another container, so this
    sets a flag its next heartbeat reads. Reporting it as already dead would be
    claiming something that has not happened yet.
    """
    _visible_run(session, user, run_id)
    return {"ok": True, "stopped": runqueue.request_cancel(session, run_id)}


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
