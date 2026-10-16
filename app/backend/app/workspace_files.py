"""Phase 3: the file and git routes, through the project's workspace agent.

With ``BLPL_FILES_VIA_WORKSPACE=1``, the routes below stop touching project
files themselves and ask the workspace's agent (sandbox/agent.py) instead:

- the tree and the editor: tree, file list, read, write;
- creating a folder, blobs and uploads (quarantine runs in the workspace);
- every commit a route makes, and git status, diff, pull and push.

The agent opens every path without following a link, runs git there with the
identity and the one credential the operation needs, and refuses writes while
a run is in progress. Off by default, and with it off nothing here is reached.

Phase 4 moved the rest of the file manager here too: delete and move with the
ledger and ignore-list bookkeeping beside the files, project libraries (library
uploads, symbol removal), and the PDF verdict routes, which read the bytes
they judge through the agent. docs/architecture-v2.md says what removing the
mount entirely still needs.

Each function here is called by the route it is named after, after that route
has done its own permission and argument checks. The helpers it shares with
the routes come from ``main`` at call time, which is why ``main`` is imported
inside functions.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

from fastapi import HTTPException
from fastapi.responses import Response

from .projects import ProjectError
from .sandbox import workspaces
from .sandbox.agentclient import AgentCallError, AgentClient

enabled = workspaces.enabled


def mount_absent() -> bool:
    """Whether this backend runs without the project mount (phase 4).

    Only with the agent path on: a backend with neither has no way to reach a
    project at all. Routes that go through the agent pass agent_ready to
    main._project_dir; any other route answers 503 in this mode instead of
    reading a directory that is not there."""
    import os

    return enabled() and os.environ.get("BLPL_PROJECT_MOUNT", "") == "absent"


class MountMisconfigured(RuntimeError):
    """BLPL_PROJECT_MOUNT says one thing and the container's mounts another."""




def _unescape(field: str) -> str:
    # mountinfo writes space, tab, newline and backslash as octal escapes.
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _backing_mount(path: str, mountinfo: str) -> tuple[str, str, str] | None:
    """The mount path lies on: its root within its filesystem, its mount
    point, and its filesystem type."""
    best, found = -1, None
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        point = _unescape(fields[4])
        inside = path == point or path.startswith(point.rstrip("/") + "/")
        # Longest mount point wins; of two at one point, the later is on top.
        if inside and len(point) >= best:
            best, found = len(point), (_unescape(fields[3]), point, right.split()[0])
    return found


def projects_visible(root: Path, mountinfo: str | None = None) -> bool:
    """Whether this container can see the host's project directories at root.

    Anything in the directory means yes, whatever it is mounted from. An empty
    one is visible when it lies on a host filesystem, because that is where
    the next project will appear. It is hidden when a fresh tmpfs is mounted
    on root itself (the mountless layout's mask), or when nothing is mounted on it or
    above it but the container's root (the image's own layer). The type of
    the bind above it says nothing: a host's data directory may be a tmpfs
    too. Where mountinfo cannot be read, an empty directory counts as hidden
    only if something is mounted on it."""
    try:
        if any(Path(root).iterdir()):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return True  # cannot tell, so do not claim it is hidden
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text()
        except OSError:
            import os

            return not os.path.ismount(root)
    path = str(Path(root).resolve())
    found = _backing_mount(path, mountinfo)
    if found is None:
        return True
    source, point, fstype = found
    # A fresh tmpfs is mounted from its own root; a bind of a host directory
    # that happens to live on a tmpfs is mounted from a path inside it.
    return not ((point == path and fstype == "tmpfs" and source == "/") or point == "/")


def check_project_mount(root: Path, *, mountinfo: str | None = None) -> str | None:
    """Refuse to start when BLPL_PROJECT_MOUNT=absent is not what the mounts say.

    The flag alone removed nothing: Compose still bound all of ./data into the
    backend (Codex, PR #20). A backend told the mount is absent that can in
    fact read every project has a guarantee on paper only, so it does not
    start. The opposite, a backend that expects the mount and finds none, would
    answer every remaining route with an empty project; that returns a warning
    for the caller to log, because a fresh deployment can legitimately look
    like it.
    """
    import os

    absent = os.environ.get("BLPL_PROJECT_MOUNT", "") == "absent"
    visible = projects_visible(root, mountinfo)
    if absent and not enabled():
        raise MountMisconfigured(
            "BLPL_PROJECT_MOUNT=absent needs BLPL_FILES_VIA_WORKSPACE=1: without the "
            "workspace agent this backend has no way to reach a project")
    if absent and visible:
        raise MountMisconfigured(
            f"BLPL_PROJECT_MOUNT=absent, but {root} is visible in this container. Start "
            "the backend with app/docker-compose.mountless.yml (see "
            "docs/architecture-v2.md, \"Running the backend without the project mount\"), "
            "or unset BLPL_PROJECT_MOUNT")
    if not absent and not visible:
        return (f"{root} is not visible in this container, but BLPL_PROJECT_MOUNT is not "
                "'absent': every route that reads a project directly will find it empty. "
                "Set BLPL_PROJECT_MOUNT=absent with BLPL_FILES_VIA_WORKSPACE=1, or restore "
                "the ./data mount")
    return None

_ACCESS: workspaces.Access | None = None
# Agent answers passed to the client as they are. Anything else is the
# workspace failing, which the client sees as 502.
_PASS_THROUGH = {400, 403, 404, 409, 413}


def _access() -> workspaces.Access:
    global _ACCESS
    if _ACCESS is None:
        from . import main as m
        from . import serverkey
        from .sandbox.docker import DockerConfig, DockerDriver

        key = serverkey.load(m._DATA)
        if key is None:
            raise HTTPException(status_code=503, detail="no server key; workspaces cannot be reached")
        try:
            _ACCESS = workspaces.Access.from_env(
                key, lambda: DockerDriver(DockerConfig.from_env()))
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=f"workspaces are misconfigured: {exc}")
    return _ACCESS


def project_of(checkout: Path) -> str:
    """The project a checkout belongs to: <root>/<P> or <root>/.worktrees/<P>/<member>."""
    from . import main as m

    parts = Path(checkout).relative_to(m.PROJECTS_ROOT).parts
    return parts[1] if parts[0] == ".worktrees" else parts[0]


def _run(coro):
    """Run a coroutine to completion from sync code, inside an event loop or not.

    Most routes here are plain functions on the threadpool, where asyncio.run
    is fine. A commit can also happen inside the upload route, which is async,
    and asyncio.run refuses to nest. So the coroutine always gets a thread and
    a loop of its own.
    """
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            box["error"] = exc

    t = threading.Thread(target=target, name="blpl-workspace-ensure")
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _connect(checkout: Path) -> AgentClient:
    try:
        return _run(_access().ensure(project_of(checkout)))
    except HTTPException:
        raise
    except workspaces.WorkspaceSealed as exc:
        # Sealed by a seal this request waited for: the same answer as a
        # project that was sealed when the request arrived.
        project = project_of(checkout)
        raise HTTPException(status_code=423, detail=str(exc),
                            headers={"X-BLPL-Sealed": project}) from exc
    except Exception as exc:  # noqa: BLE001 - said to the client, not lost
        raise HTTPException(status_code=503,
                            detail=f"the project's workspace is not available: {exc}") from exc


@contextmanager
def gated(checkout: Path) -> Iterator[None]:
    """The project's gate (sandbox/gate.py), shared, for one whole operation
    through the agent: the start, the call, and anything done under it.

    Holding it only while the workspace started let a seal take the gate
    between the start and the operation, stop the workspace and archive a
    half-written file or a git index.lock (Codex, PR #20). A seal waits a few
    seconds for operations in flight, then reports busy and is retried.

    Taken again by the start inside (Access.ensure) on another descriptor:
    flock grants a second shared lock while one is held, and a seal never
    queues for the exclusive lock in the kernel (it polls), so a shared
    holder is never stuck behind one. Nothing that holds it seals.
    """
    from . import main as m
    from .sandbox import gate

    try:
        with gate.held(m.PROJECTS_ROOT, project_of(checkout), exclusive=False,
                       wait_s=gate.START_WAIT_S):
            yield
    except gate.GateBusy as exc:
        raise HTTPException(status_code=503,
                            detail=f"the project is being sealed or opened: {exc}") from exc


@contextmanager
def agent(checkout: Path) -> Iterator[AgentClient]:
    with gated(checkout):
        client = _connect(checkout)
        try:
            yield client
        except AgentCallError as exc:
            raise HTTPException(status_code=_status(exc), detail=str(exc)) from exc
        finally:
            client.close()


def _status(exc: AgentCallError) -> int:
    """The route's status for an agent's refusal.

    The agent refuses every path through a link, and any `..` or `.git`, with
    403. The routes have always answered a path that leaves the project with
    400, so that contract holds whichever side checked. A 500 the agent meant
    (a move that half-failed) passes through; one it did not mean is the
    workspace failing, 502."""
    if exc.status == 403 and exc.kind == "forbidden":
        return 400
    if exc.status in _PASS_THROUGH or (exc.status == 500 and exc.kind != "error"):
        return exc.status
    return 502


def _cred(env: dict[str, str] | None) -> dict[str, Any] | None:
    """A leased credential, rewritten for the agent.

    An HTTPS token rides the environment and passes through as it is. An ssh
    key was leased as a file on this side, named by GIT_SSH_COMMAND. That file
    does not exist in the workspace, so its content goes instead. The agent
    writes it to its own tmpfs for the one command and deletes it.
    """
    if not env:
        return None
    env = dict(env)
    ssh = env.pop("GIT_SSH_COMMAND", None)
    if ssh is None:
        return {"env": env}
    m = re.search(r"-i (\S+)", ssh)
    if not m:
        return {"env": {**env, "GIT_SSH_COMMAND": ssh}}
    return {"env": env, "ssh_key": Path(m.group(1)).read_text()}


# -- tree and editor --------------------------------------------------------------


def _ignored(client: AgentClient, checkout: Path) -> set[str]:
    from . import llm_ignore

    try:
        data = json.loads(client.read(str(checkout), str(llm_ignore.LIST_PATH)))
    except (AgentCallError, ValueError):
        return set()
    rows = data.get("files", []) if isinstance(data, dict) else []
    return {llm_ignore._norm(r["path"]) for r in rows if isinstance(r, dict) and r.get("path")}


def tree(project_id: str, checkout: Path) -> dict:
    from . import main as m

    with agent(checkout) as c:
        listing = c.list(str(checkout), skip=sorted(m._TREE_SKIP), max_depth=m._TREE_MAX_DEPTH)
        ignored = _ignored(c, checkout)
        recoverable = c.git_recoverable(str(checkout))
    nodes = []
    for e in listing["entries"]:
        node: dict[str, Any] = {"path": e["path"], "name": e["name"], "dir": e["dir"],
                                "role": m._tree_role(Path(e["path"]))}
        if e["dir"]:
            if e.get("incomplete"):
                node["incomplete"] = True
        else:
            node["bytes"] = e["bytes"]
            node["modified"] = datetime.fromtimestamp(e["mtime"], tz=timezone.utc).isoformat()
            if node["path"] in ignored:
                node["llm_ignore"] = True
            if recoverable is not None and node["path"] not in recoverable:
                node["untracked"] = True
        nodes.append(node)
    return {"project_id": project_id, "nodes": nodes, "git": recoverable is not None}


def list_files(checkout: Path) -> list[dict]:
    from . import main as m

    with agent(checkout) as c:
        listing = c.list(str(checkout), skip=sorted(m._TREE_SKIP), max_depth=64)
    out = []
    for e in listing["entries"]:
        parts = e["path"].split("/")
        if e["dir"] or any(p.startswith(".") for p in parts):
            continue
        if Path(e["path"]).suffix.lower() in m._EDITABLE_SUFFIXES:
            out.append({"name": e["path"], "bytes": e["bytes"]})
    return sorted(out, key=lambda r: r["name"])


def read_file(checkout: Path, rel: str, name: str) -> dict:
    with agent(checkout) as c:
        try:
            data = c.read(str(checkout), rel)
        except AgentCallError as exc:
            if exc.status == 404:
                raise HTTPException(status_code=404, detail=f"no file {name!r}") from exc
            raise
    return {"name": name, "content": data.decode("utf-8", errors="replace")}


def write_file(checkout: Path, rel: str, content: str) -> int:
    with agent(checkout) as c:
        return c.write(str(checkout), rel, content.encode("utf-8"), mkdirs=True)["bytes"]


def make_folder(checkout: Path, rel: str, shown: str) -> None:
    with agent(checkout) as c:
        try:
            c.mkdir(str(checkout), rel)
        except AgentCallError as exc:
            if exc.status == 409:
                raise HTTPException(status_code=409, detail=f"{shown} already exists") from exc
            raise


class _AgentFile:
    """One file in the workspace, read through the agent a block at a time.

    What main._file_response and main._spool_and_hash need of a file object
    (read, seek, close), so the blob route streams a file of any size, and a
    range of one, the way the mounted route streams from its descriptor.

    Every read after the first names the version of the file the first one
    saw, so the agent refuses (409) rather than hand over bytes of a file
    replaced or rewritten since: everything sent is one file's bytes, and a
    file classified by its first block is the file the rest comes from. One
    client for the whole stream, but neither a connection to the agent nor
    the gate is held between blocks: a slow download does not take one of
    the agent's connection slots or hold a seal off, and a seal between
    blocks ends the download rather than waiting for it.
    """

    BLOCK = 1024 * 1024

    def __init__(self, checkout: Path, rel: str):
        self.checkout, self.rel = checkout, rel
        with gated(checkout):
            self._client = _connect(checkout)
        try:
            self._buf, self.size, self._ident = self._fetch(0, None)
        except BaseException:
            self._client.close()
            raise
        self._at, self._pos = 0, 0

    def _fetch(self, offset: int, expect: str | None) -> tuple[bytes, int, str]:
        try:
            with gated(self.checkout):
                if self._client.version() < 2:
                    # A workspace an older backend started, not yet replaced
                    # (agentclient.PROTOCOL): its agent has no ranged read, so
                    # the file comes whole, as one block, as it did then. Over
                    # twice the upload limit that is a 413 until it is.
                    from . import main as m

                    data = self._client.read(str(self.checkout), self.rel,
                                             max_bytes=m.UPLOAD_MAX_BYTES * 2)
                    return data, len(data), ""
                return self._client.read_range(str(self.checkout), self.rel, offset,
                                               self.BLOCK, expect=expect)
        except AgentCallError as exc:
            if exc.status == 404:
                raise HTTPException(status_code=404,
                                    detail=f"{self.rel} is not a file in this project") from exc
            raise HTTPException(status_code=_status(exc), detail=str(exc)) from exc

    @property
    def head(self) -> bytes:
        return self._buf[:1024] if self._at == 0 else b""

    def seek(self, pos: int) -> None:
        self._pos = pos

    def read(self, n: int = -1) -> bytes:
        if self._pos >= self.size:
            return b""
        if not self._at <= self._pos < self._at + len(self._buf):
            self._buf, _, _ = self._fetch(self._pos, self._ident)
            self._at = self._pos
            if not self._buf:
                return b""
        i = self._pos - self._at
        out = self._buf[i:] if n is None or n < 0 else self._buf[i:i + n]
        self._pos += len(out)
        return out

    def close(self) -> None:
        self._client.close()


def blob(checkout: Path, rel: str, request=None, verdict_for=None) -> Response:
    """The bytes of one file, with the same disposition rules as the mounted route.

    A PDF goes to the browser's viewer only if these exact bytes have a clean
    verdict from this backend's own inspection, as on the mounted route; any
    other PDF is an opaque download. A PDF within the upload limit is spooled
    through the agent and hashed, and the spool that was hashed is what is
    sent, so a file swapped in the workspace afterwards goes out under no
    verdict at all. Anything else, a PDF over the limit included, is streamed
    from the workspace as it is read (_AgentFile), with Range honoured.
    """
    from blpl.core import quarantine

    from . import main as m

    parts = tree_rel(rel).split("/")
    name = parts[-1]
    fh = _AgentFile(checkout, "/".join(parts))
    try:
        if parts[0] == quarantine.QUARANTINE_DIRNAME:
            return m._file_response(request, fh, fh.size, "application/octet-stream", {
                "Content-Disposition": m._disposition("attachment", name),
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": m._INERT_CSP,
                "X-BLPL-Quarantined": "1",
            })
        if not quarantine.looks_like_pdf(fh.head):
            media_type, inline = m._blob_disposition(Path(name))
            return m._file_response(request, fh, fh.size, media_type, {
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": m._disposition("inline" if inline else "attachment", name),
                "Content-Security-Policy": m._INERT_CSP,
            })
        if fh.size > m.UPLOAD_MAX_BYTES or verdict_for is None:
            # Never judged, so never inline: streamed as an attachment with no
            # validator, as the mounted route sends a PDF over the limit.
            return m._file_response(request, fh, fh.size, "application/octet-stream",
                                    m._opaque_headers(name, "too_large" if verdict_for else "none"))
        spool, digest, total = m._spool_and_hash(fh, m.UPLOAD_MAX_BYTES)
    except BaseException:
        fh.close()
        raise
    fh.close()
    try:
        # Not expected: every block was read from the version the size came
        # from. Said rather than judged if it happens.
        if total != fh.size:
            raise HTTPException(status_code=409,
                                detail=f"{name} changed while it was being read; try again")
        verdict = verdict_for(digest)
    except BaseException:
        spool.close()
        raise
    return m._pdf_response(request, spool, total, digest, name, verdict)


async def accept_upload(checkout: Path, data: bytes, **meta: Any) -> dict:
    """quarantine.accept_upload, run in the workspace: inspection and scanning
    of an untrusted file happen beside the project, not in the control plane."""
    import os

    from .sandbox.runpath import requires_scan

    # Said on every request, not left to the workspace's environment: the
    # control plane's policy decides, and a workspace older than this setting,
    # or one that cannot reach the scanner, then holds rather than releases.
    if requires_scan(os.environ):
        meta["require_scan"] = "1"
    def upload() -> dict:
        # In one thread, so the gate is held for the whole upload: an flock
        # belongs to the descriptor, and the context opens and closes it here.
        with agent(checkout) as c:
            return c.accept_upload(str(checkout), data, **meta)

    return await asyncio.to_thread(upload)


# -- git ---------------------------------------------------------------------------


def git_status(checkout: Path) -> SimpleNamespace:
    with agent(checkout) as c:
        try:
            return SimpleNamespace(**c.git_status(str(checkout)))
        except AgentCallError as exc:
            if exc.kind == "project":
                raise ProjectError(str(exc)) from exc
            raise


def git_diff(checkout: Path) -> dict:
    with agent(checkout) as c:
        try:
            return c.git_diff(str(checkout))
        except AgentCallError as exc:
            if exc.kind == "project":
                raise ProjectError(str(exc)) from exc
            raise


def remote_of(checkout: Path, operation: str) -> str:
    with agent(checkout) as c:
        try:
            return c.git_remote_of(str(checkout), operation)
        except AgentCallError as exc:
            if exc.kind == "project":
                raise ProjectError(str(exc)) from exc
            raise


def _project_of(checkout: Path) -> str:
    from . import main as m

    rel = Path(checkout).relative_to(m.PROJECTS_ROOT).parts
    return rel[1] if rel[0] == ".worktrees" else rel[0]


@contextmanager
def _git_proxy(checkout: Path, remote: str | None = None) -> Iterator[dict[str, str]]:
    """The egress proxy's credential for one git operation (egress.py): the
    shipped defaults and the administrators' hosts, plus, on port 22, the host
    of the remote this operation uses when it is an ssh one, revoked when the
    operation ends. Nothing without an egress proxy."""
    import os

    from . import egress
    from .db import SessionFactory

    if not os.environ.get("BLPL_SANDBOX_HTTP_PROXY"):
        yield {}
        return
    ssh_host = egress.ssh_remote_host(remote)
    with SessionFactory() as session:
        name, token = egress.issue(session, kind="git", project=_project_of(checkout),
                                   ssh_hosts=[ssh_host] if ssh_host else (),
                                   ttl=egress.GIT_GRANT_TTL)
        session.commit()
    try:
        yield egress.proxy_env(name, token)
    finally:
        with SessionFactory() as session:
            egress.revoke(session, name)
            session.commit()


def _remote(c: AgentClient, checkout: Path, url: str | None, operation: str) -> str | None:
    """The remote this operation will use: the one passed, else the
    project's configured one, read in the workspace. Only for the grant's
    ssh host, so a remote that cannot be read is no ssh host, not an error."""
    import os

    if url or not os.environ.get("BLPL_SANDBOX_HTTP_PROXY"):
        return url
    try:
        return c.git_remote_of(str(checkout), operation) or None
    except AgentCallError:
        return None


def pull(checkout: Path, url: str, env: dict[str, str] | None) -> str:
    with agent(checkout) as c, _git_proxy(checkout, _remote(c, checkout, url, "fetch")) as proxy:
        try:
            return c.git_pull(str(checkout), url, _cred(env), proxy)
        except AgentCallError as exc:
            if exc.kind == "project":
                raise ProjectError(str(exc)) from exc
            raise


class _AgentProjects:
    """The part of Projects that pushes and records, routed to the agent.

    Project setup records a push's outcome while the origin lock is held
    (``Projects.push(after=...)``, ``Projects.under_remote_lock``), so that
    overlapping pushes record in the order they ran. The lock is in the
    project's git directory and the agent takes it. A held push answers with
    the outcome while still holding it; ``after`` runs here, recording in the
    control plane's database, and only then is the lease released. The order
    guarantee is the same as with the mount.
    """

    @staticmethod
    @contextmanager
    def _client(checkout: Path) -> Iterator[AgentClient]:
        """A client, under the project's gate for the whole operation."""
        try:
            with gated(checkout):
                c = _connect(checkout)
                try:
                    yield c
                finally:
                    c.close()
        except HTTPException as exc:
            raise ProjectError(str(exc.detail)) from exc

    def push(self, name: str, *, env: dict[str, str] | None = None,
             checkout: Path, url: str | None = None, after=None) -> str:
        with self._client(checkout) as c:
            return self._push(c, checkout, env, url, after)

    def _push(self, c: AgentClient, checkout: Path, env, url, after) -> str:
        try:
            with _git_proxy(checkout, _remote(c, checkout, url, "push")) as proxy:
                r = c.git_push_held(str(checkout), url or "", _cred(env), proxy)
        except AgentCallError as exc:
            raise ProjectError(str(exc)) from exc
        exc = ProjectError(r["error"]) if r.get("error") is not None else None
        lease = r.get("lease")
        if lease is None:
            # The lock was never had, so nothing ran and nothing is
            # recorded under it, exactly as Projects.push raising busy.
            raise exc or ProjectError("the origin lock could not be taken")
        try:
            if after is not None:
                after(exc)
        finally:
            c.git_release(lease)
        if exc is not None:
            raise exc
        return r["output"]

    def under_remote_lock(self, checkout: Path, fn):
        with self._client(checkout) as c:
            r = c.git_hold(str(checkout))
            if r.get("lease") is None:
                raise ProjectError(r.get("error") or "the origin lock could not be taken")
            try:
                return fn()
            finally:
                c.git_release(r["lease"])


AGENT_PROJECTS = _AgentProjects()


def push(checkout: Path, url: str, env: dict[str, str] | None, *, after=None) -> str:
    """main.git_push's push, through the agent, recording under the lock."""
    return AGENT_PROJECTS.push("", env=env, checkout=checkout, url=url, after=after)


def commit_checkout(checkout: Path, message: str, identity) -> str | None:
    """projects.commit_checkout, made by the agent: the member's identity rides
    that one git command, which the agent's copy of Projects._git requires of
    every commit, exactly as the backend's does."""
    with agent(checkout) as c:
        try:
            return c.git_commit(str(checkout), message,
                                identity.as_json() if identity is not None else None)
        except AgentCallError as exc:
            if exc.kind == "project":
                raise ProjectError(str(exc)) from exc
            raise


# -- phase 4: the rest of the file manager, records, boards, worktrees -------------


def _project_errors(exc: AgentCallError) -> None:
    if exc.kind == "project":
        raise ProjectError(str(exc)) from exc


def ensure_worktree(project_id: str, user_id: int, is_owner: bool) -> Path:
    """main._project_dir's worktrees.ensure, done in the project's workspace."""
    from . import main as m

    with agent(m.PROJECTS_ROOT / project_id) as c:
        try:
            r = c.worktree_ensure(project_id, user_id, is_owner)
        except AgentCallError as exc:
            _project_errors(exc)
            raise
    if not r["exists"]:
        raise HTTPException(
            status_code=409, detail=f"project {project_id!r} is registered but has no working copy")
    return Path(r["path"])


def remove_worktree(project_id: str, user_id: int) -> bool:
    """main.remove_member's worktrees.remove, done in the project's workspace."""
    from . import main as m

    with agent(m.PROJECTS_ROOT / project_id) as c:
        try:
            return bool(c.worktree_remove(project_id, user_id)["removed"])
        except AgentCallError as exc:
            _project_errors(exc)
            raise


def managed_rel(name: str) -> str:
    """The lexical rules of main._managed_path. Whether a parent is a link
    is the agent's to refuse, and it refuses every link."""
    parts = [seg for seg in name.split("/") if seg]
    if (not parts or "\\" in name or name.startswith("/")
            or any(seg == ".." or seg.startswith(".") for seg in parts)):
        raise HTTPException(status_code=400, detail=f"invalid path {name!r}")
    if "retrieved" in parts:
        raise HTTPException(status_code=400, detail=(
            "files under retrieved/ are held by quarantine and kept as "
            "evidence — they cannot be deleted or moved from here"))
    return "/".join(parts)


def delete(checkout: Path, rel: str, by: str = "") -> dict:
    with agent(checkout) as c:
        return c.managed_delete(str(checkout), rel, by)


def move(checkout: Path, src: str, dst: str, by: str = "") -> dict:
    with agent(checkout) as c:
        return c.managed_move(str(checkout), src, dst, by)


# -- project libraries ---------------------------------------------------------------
#
# library_upload works on the checkout's libraries/, so the agent runs it there.
# The control plane still decides who may and which names are legal; the agent
# refuses a libraries/ tree with a link in it, which the mounted route's
# sandbox would have resolved and contained.


def check_library(checkout: Path, name: str, suggest: str) -> str:
    """library_upload.check_library, against the project's own libraries."""
    with agent(checkout) as c:
        return c.library_check(str(checkout), name, suggest)


def library_names(checkout: Path) -> list[str]:
    with agent(checkout) as c:
        return c.library_names(str(checkout))


def library_symbols(checkout: Path, library: str) -> dict:
    with agent(checkout) as c:
        return c.library_symbols(str(checkout), library)


def remove_library_symbol(checkout: Path, library: str, symbol: str, by: str) -> dict:
    with agent(checkout) as c:
        return c.library_remove_symbol(str(checkout), library, symbol, by)


# -- PDF verdicts -------------------------------------------------------------------


def tree_rel(rel: str) -> str:
    """A tree path's lexical rules, as the blob route applies them. Links are
    the agent's to refuse, and it refuses every one."""
    parts = [p for p in rel.split("/") if p]
    if not parts or rel.startswith("/") or "\\" in rel or ".." in parts:
        raise HTTPException(status_code=400, detail="path is outside the project")
    return "/".join(parts)


def pdf_bytes(checkout: Path, rel: str, cap: int) -> tuple[bytes, int, bytes | None]:
    """What the verdict routes judge a file by: its first kilobyte, its size,
    and, for a PDF within ``cap``, its whole content, read once by the agent.

    The content is what gets hashed and inspected, so a verdict is about the
    bytes in hand, never about a path. None when the file is not a PDF or is
    over the cap (by size, or by the read: the agent refuses past ``cap``).
    """
    from blpl.core import quarantine

    with agent(checkout) as c:
        try:
            st = c.stat(str(checkout), rel)
            if not st.get("exists") or st.get("dir"):
                raise HTTPException(status_code=404, detail=f"{rel} is not a file in this project")
            head = c.read(str(checkout), rel, head=1024)
            data = None
            if quarantine.looks_like_pdf(head) and st["bytes"] <= cap:
                try:
                    data = c.read(str(checkout), rel, max_bytes=cap)
                except AgentCallError as exc:
                    if exc.status != 413:
                        raise
        except AgentCallError as exc:
            if exc.status == 404:
                raise HTTPException(status_code=404,
                                    detail=f"{rel} is not a file in this project") from exc
            raise
    return head, st["bytes"], data


def ledger_record(checkout: Path, sha256: str) -> dict | None:
    """quarantine.record_for, read in the workspace (pdf_verdicts.lookup)."""
    with agent(checkout) as c:
        return c.quarantine_record(str(checkout), sha256)


def ignore_entries(checkout: Path) -> list[dict]:
    with agent(checkout) as c:
        return c.ignore_entries(str(checkout))


def ignore_set(checkout: Path, path: str, ignored: bool, by: str) -> dict:
    with agent(checkout) as c:
        return c.ignore_set(str(checkout), path, ignored, by)


def quarantine_view(checkout: Path) -> dict:
    with agent(checkout) as c:
        return c.quarantine_view(str(checkout))


def manifest(checkout: Path):
    """What main._resolve_board reads from project_manifest.discover."""
    from blpl.core import project_manifest

    with agent(checkout) as c:
        try:
            r = c.project_boards(str(checkout))
        except AgentCallError as exc:
            if exc.kind == "manifest":
                raise project_manifest.ManifestError(str(exc)) from exc
            raise
    boards = [SimpleNamespace(name=n) for n in r["boards"]]
    return SimpleNamespace(implicit=r["implicit"], boards=boards,
                           board=lambda n: next((b for b in boards if b.name == n), None))


def list_artifacts(checkout: Path) -> list[dict]:
    """The files directly in .pipeline/, as main._artifact_meta describes them."""
    with agent(checkout) as c:
        try:
            listing = c.list(str(checkout), ".pipeline", max_depth=0)
        except AgentCallError as exc:
            if exc.status == 404:
                return []
            raise
    out = []
    for e in listing["entries"]:
        if e["dir"] or "/" in e["path"][len(".pipeline/"):]:
            continue
        ts = e.get("btime") or e["mtime"]
        out.append({"name": e["name"], "size": e["bytes"],
                    "created": datetime.fromtimestamp(ts, tz=timezone.utc)
                    .strftime("%Y-%m-%d_%H%M%SZ")})
    return out


def read_artifact(checkout: Path, name: str) -> bytes | None:
    parts = [p for p in name.split("/") if p]
    if not parts or ".." in parts or name.startswith("/"):
        return None
    with agent(checkout) as c:
        try:
            return c.read(str(checkout), ".pipeline/" + "/".join(parts))
        except AgentCallError as exc:
            if exc.status in (403, 404):
                return None
            raise


# -- phase 4: sealing in a sealer container ---------------------------------------


def seal_errors() -> tuple[type[BaseException], ...]:
    """What a seal or unseal through a sealer container can fail with, as the
    routes treat a WorkspaceError: the sealer's own refusal, and the driver
    failing to run it."""
    from .sandbox.driver import DriverError
    from .sandbox.sealer import SealError

    return (SealError, DriverError)


def seal(project: str, key: bytes, *, gate_held: bool = False) -> int:
    """workspace.seal, in a sealer container: the backend needs no mount."""
    from .sandbox.sealer import run_sealer

    access = _access()
    return _run(run_sealer(access.driver_factory(), access.run_cfg, project, key,
                           "seal", gate_held=gate_held))["sealed_bytes"]


def unseal(project: str, key: bytes) -> None:
    from .sandbox.sealer import run_sealer

    access = _access()
    _run(run_sealer(access.driver_factory(), access.run_cfg, project, key, "unseal"))


_TRACKER = None


async def sweep_and_seal(seal_project) -> list:
    """The workspace idle sweep, run where the keys are, sealing what it stops.

    The worker's sweep cannot seal: it has no project key. So with workspaces
    behind the agent, the backend sweeps. A workspace the agent reports idle
    (no request, no run) is stopped, and its project is sealed through the
    on_stopped hook."""
    from .sandbox import idle
    from .sandbox.sealer import on_stopped_sealer

    global _TRACKER
    if _TRACKER is None:
        _TRACKER = idle.IdleTracker()
    access = _access()
    driver = access.driver_factory()

    async def activity(key):
        return await access.agent_idle(key.container_name())

    try:
        return await idle.sweep(driver, _TRACKER, idle_after_s=access.run_cfg.idle_after_s,
                                activity=activity,
                                on_stopped=on_stopped_sealer(seal_project),
                                gate_root=access.run_cfg.projects_root)
    finally:
        close = getattr(driver, "aclose", None)
        if close is not None:
            await close()


def set_remote(checkout: Path, url: str) -> None:
    """main.set_git_remote's change, made by the agent, so every taker of the
    origin lock is inside the workspace (gVisor keeps file locks in-sandbox)."""
    with agent(checkout) as c:
        try:
            c.git_set_remote(str(checkout), url)
        except AgentCallError as exc:
            _project_errors(exc)
            raise
