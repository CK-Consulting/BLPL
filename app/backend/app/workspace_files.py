"""Phase 3: the file and git routes, through the project's workspace agent.

With ``BLPL_FILES_VIA_WORKSPACE=1``, the routes below stop touching project
files themselves and ask the workspace's agent (sandbox/agent.py) instead:

- the tree and the editor: tree, file list, read, write;
- creating a folder, blobs and uploads (quarantine runs in the workspace);
- every commit a route makes, and git status, diff, pull and push.

The agent opens every path without following a link, runs git there with the
identity and the one credential the operation needs, and refuses writes while
a run is in progress. Off by default, and with it off nothing here is reached.

What still uses the backend's own mount with the flag on is resolving the
checkout: the permission check, the sealed check, and creating a member's
worktree on first use (``_project_dir``). Delete and move use it too, because
their guards read the quarantine ledger and the ignore list as files.
docs/architecture-v2.md, phase 3, says what removing the mount entirely still
needs.

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
    except Exception as exc:  # noqa: BLE001 - said to the client, not lost
        raise HTTPException(status_code=503,
                            detail=f"the project's workspace is not available: {exc}") from exc


@contextmanager
def agent(checkout: Path) -> Iterator[AgentClient]:
    client = _connect(checkout)
    try:
        yield client
    except AgentCallError as exc:
        raise HTTPException(status_code=exc.status if exc.status in _PASS_THROUGH else 502,
                            detail=str(exc)) from exc
    finally:
        client.close()


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


def blob(checkout: Path, rel: str) -> Response:
    """The bytes of one file, with the same disposition rules as the mounted route."""
    from blpl.core import quarantine

    from . import main as m

    parts = [p for p in rel.split("/") if p]
    if not parts or rel.startswith("/") or "\\" in rel or ".." in parts:
        raise HTTPException(status_code=400, detail="path is outside the project")
    name = parts[-1]
    with agent(checkout) as c:
        data = c.read(str(checkout), "/".join(parts), max_bytes=m.UPLOAD_MAX_BYTES * 2)
    if parts[0] == quarantine.QUARANTINE_DIRNAME:
        return Response(data, media_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
            "X-BLPL-Quarantined": "1",
        })
    media_type, inline = m._blob_disposition(Path(name))
    return Response(data, media_type=media_type, headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{name}"',
    })


async def accept_upload(checkout: Path, data: bytes, **meta: Any) -> dict:
    """quarantine.accept_upload, run in the workspace: inspection and scanning
    of an untrusted file happen beside the project, not in the control plane."""
    client = await asyncio.to_thread(_connect, checkout)
    try:
        return await asyncio.to_thread(client.accept_upload, str(checkout), data, **meta)
    except AgentCallError as exc:
        raise HTTPException(status_code=exc.status if exc.status in _PASS_THROUGH else 502,
                            detail=str(exc)) from exc
    finally:
        client.close()


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


def pull(checkout: Path, url: str, env: dict[str, str] | None) -> str:
    with agent(checkout) as c:
        try:
            return c.git_pull(str(checkout), url, _cred(env))
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
    def _client(checkout: Path) -> AgentClient:
        try:
            return _connect(checkout)
        except HTTPException as exc:
            raise ProjectError(str(exc.detail)) from exc

    def push(self, name: str, *, env: dict[str, str] | None = None,
             checkout: Path, url: str | None = None, after=None) -> str:
        c = self._client(checkout)
        try:
            try:
                r = c.git_push_held(str(checkout), url or "", _cred(env))
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
        finally:
            c.close()

    def under_remote_lock(self, checkout: Path, fn):
        c = self._client(checkout)
        try:
            r = c.git_hold(str(checkout))
            if r.get("lease") is None:
                raise ProjectError(r.get("error") or "the origin lock could not be taken")
            try:
                return fn()
            finally:
                c.git_release(r["lease"])
        finally:
            c.close()


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

