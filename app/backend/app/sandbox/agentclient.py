"""The control plane's side of blpl-wsagent: tokens and a client.

**Tokens.** One per container start, never stored. The control plane writes a
random nonce to ``<agent dir>/agent.nonce`` and the token
``HMAC-SHA256(server key, "blpl-wsagent" | container | nonce)`` to
``agent.token``, then starts the container. The agent reads the token and
unlinks it. Any control-plane process holding the server key (backend or
worker) can recompute the token from the nonce, which is not secret. Nothing
inside the workspace can, because the server key is not there.

``prepare`` takes an exclusive lock on the agent directory, so two control-plane
processes starting the same workspace cannot leave it holding one token while
the nonce describes another.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import httpx

TOKEN_FILE = "agent.token"
NONCE_FILE = "agent.nonce"
SOCKET_FILE = "agent.sock"
LOCK_FILE = "agent.lock"


class AgentCallError(RuntimeError):
    def __init__(self, status: int, message: str, kind: str = "error"):
        super().__init__(message)
        self.status, self.kind = status, kind


def derive_token(server_key: bytes, container: str, nonce: str) -> str:
    msg = b"blpl-wsagent\0" + container.encode() + b"\0" + nonce.encode()
    return hmac.new(server_key, msg, hashlib.sha256).hexdigest()


@contextmanager
def locked(agent_dir: Path) -> Iterator[None]:
    agent_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(agent_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def prepare(agent_dir: Path, server_key: bytes, container: str) -> str:
    """Leave a fresh token for the next start. Call with ``locked`` held."""
    nonce = secrets.token_hex(16)
    token = derive_token(server_key, container, nonce)
    for name, value in ((TOKEN_FILE, token), (NONCE_FILE, nonce)):
        tmp = agent_dir / f".{name}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value + "\n")
        os.replace(tmp, agent_dir / name)
    # A stale socket from the previous start would answer nothing.
    try:
        (agent_dir / SOCKET_FILE).unlink()
    except FileNotFoundError:
        pass
    return token


def current_token(agent_dir: Path, server_key: bytes, container: str) -> str | None:
    try:
        nonce = (agent_dir / NONCE_FILE).read_text().strip()
    except FileNotFoundError:
        return None
    return derive_token(server_key, container, nonce)


# A push or pull waits up to Projects.NETWORK_LOCK_WAIT (330 s) for the origin
# lock, then for the network. The client's own wait for an automatic push is
# autopush.PUSH_WAIT_SECONDS (690 s); a call through the agent gets the same.
NETWORK_TIMEOUT_S = 690.0


class AgentClient:
    """Synchronous: the backend's file and git routes are plain functions."""

    def __init__(self, socket_path: Path | str, token: str, *, timeout: float = 120.0):
        self.socket_path = str(socket_path)
        self._http = httpx.Client(
            transport=httpx.HTTPTransport(uds=self.socket_path),
            base_url="http://wsagent",
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout, connect=5.0),
        )

    def close(self) -> None:
        self._http.close()

    def _check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            try:
                body = r.json()
                raise AgentCallError(r.status_code, body.get("error", r.text),
                                     body.get("kind", "error"))
            except ValueError:
                raise AgentCallError(r.status_code, r.text) from None
        return r

    def call(self, path: str, body: dict[str, Any] | None = None, *,
             timeout: float | None = None) -> Any:
        kw = {} if timeout is None else {"timeout": httpx.Timeout(timeout, connect=5.0)}
        return self._check(self._http.post(path, content=json.dumps(body or {}), **kw)).json()

    def health(self) -> dict:
        return self._check(self._http.get("/health")).json()

    # files
    def list(self, checkout: str, path: str = "", *, skip: list[str] | None = None,
             max_depth: int = 12) -> dict:
        return self.call("/fs/list", {"checkout": checkout, "path": path,
                                      "skip": skip or [], "max_depth": max_depth})

    def stat(self, checkout: str, path: str) -> dict:
        return self.call("/fs/stat", {"checkout": checkout, "path": path})

    def read(self, checkout: str, path: str, *, max_bytes: int | None = None) -> bytes:
        body: dict[str, Any] = {"checkout": checkout, "path": path}
        if max_bytes is not None:
            body["max_bytes"] = max_bytes
        return self._check(self._http.post("/fs/read", content=json.dumps(body))).content

    def write(self, checkout: str, path: str, data: bytes, *, mkdirs: bool = False) -> dict:
        r = self._http.post("/fs/write", params={"checkout": checkout, "path": path,
                                                 "mkdirs": "1" if mkdirs else "0"},
                            content=data)
        return self._check(r).json()

    def mkdir(self, checkout: str, path: str) -> dict:
        return self.call("/fs/mkdir", {"checkout": checkout, "path": path})

    def delete(self, checkout: str, path: str, *, recursive: bool = False) -> dict:
        return self.call("/fs/delete", {"checkout": checkout, "path": path,
                                        "recursive": recursive})

    def rename(self, checkout: str, src: str, dst: str, *, mkdirs: bool = False) -> dict:
        return self.call("/fs/rename", {"checkout": checkout, "src": src, "dst": dst,
                                        "mkdirs": mkdirs})

    # quarantine
    def accept_upload(self, checkout: str, data: bytes, **meta: Any) -> dict:
        params = {"checkout": checkout, **{k: str(v) for k, v in meta.items()}}
        return self._check(self._http.post("/quarantine/accept_upload", params=params,
                                           content=data)).json()

    # git
    def git_status(self, checkout: str) -> dict:
        return self.call("/git/status", {"checkout": checkout})

    def git_diff(self, checkout: str) -> dict:
        return self.call("/git/diff", {"checkout": checkout})

    def git_remote_of(self, checkout: str, operation: str) -> str:
        return self.call("/git/remote_of", {"checkout": checkout,
                                            "operation": operation})["url"]

    def git_commit(self, checkout: str, message: str,
                   identity: dict[str, str] | None) -> str | None:
        return self.call("/git/commit", {"checkout": checkout, "message": message,
                                         "identity": identity})["output"]

    def git_recoverable(self, checkout: str) -> set[str] | None:
        paths = self.call("/git/recoverable", {"checkout": checkout})["paths"]
        return None if paths is None else set(paths)

    # ``proxy`` is the egress proxy's variables with the credential the
    # control plane issued for this one operation (egress.py); the agent sets
    # them for the git command and nothing else.
    def git_pull(self, checkout: str, url: str, credential: dict | None,
                 proxy: dict | None = None) -> str:
        return self.call("/git/pull", {"checkout": checkout, "url": url, "proxy": proxy or {},
                                       **(credential or {})}, timeout=NETWORK_TIMEOUT_S)["output"]

    def git_push(self, checkout: str, url: str, credential: dict | None,
                 proxy: dict | None = None) -> str:
        return self.call("/git/push", {"checkout": checkout, "url": url, "proxy": proxy or {},
                                       **(credential or {})}, timeout=NETWORK_TIMEOUT_S)["output"]

    def git_push_held(self, checkout: str, url: str, credential: dict | None,
                      proxy: dict | None = None) -> dict:
        """Push, and keep the origin lock until git_release(lease): the
        outcome is {"output", "error", "lease"}; lease is None when the lock
        itself could not be had (nothing is held, nothing was pushed)."""
        return self.call("/git/push", {"checkout": checkout, "url": url, "hold": True,
                                       "proxy": proxy or {}, **(credential or {})},
                         timeout=NETWORK_TIMEOUT_S)

    def git_hold(self, checkout: str) -> dict:
        return self.call("/git/hold", {"checkout": checkout}, timeout=NETWORK_TIMEOUT_S)

    def git_release(self, lease: str) -> None:
        self.call("/git/release", {"lease": lease})

    # runs
    def run_start(self, argv: list[str], env: dict[str, str], cwd: str | None,
                  rid: str | None = None) -> str:
        return self.call("/runs", {"argv": argv, "env": env, "cwd": cwd, "id": rid})["id"]

    def run_read(self, rid: str, offset: int) -> tuple[bytes, int, bool, int | None]:
        r = self._check(self._http.get(f"/runs/{rid}", params={"offset": str(offset)}))
        code = r.headers.get("X-Run-Exit", "")
        return (r.content, int(r.headers["X-Run-Offset"]), r.headers.get("X-Run-Done") == "1",
                int(code) if code else None)

    def run_cancel(self, rid: str) -> None:
        self._check(self._http.post(f"/runs/{rid}/cancel"))

    # phase 4: the rest of the file manager, and the project's own records
    def managed_delete(self, checkout: str, path: str) -> dict:
        return self.call("/managed/delete", {"checkout": checkout, "path": path})

    def managed_move(self, checkout: str, src: str, dst: str) -> dict:
        return self.call("/managed/move", {"checkout": checkout, "src": src, "dst": dst})

    def ignore_entries(self, checkout: str) -> list[dict]:
        return self.call("/ignore/entries", {"checkout": checkout})["files"]

    def ignore_set(self, checkout: str, path: str, ignored: bool, by: str) -> dict:
        return self.call("/ignore/set", {"checkout": checkout, "path": path,
                                         "ignored": ignored, "by": by})

    def quarantine_view(self, checkout: str) -> dict:
        return self.call("/quarantine/view", {"checkout": checkout})

    def project_boards(self, checkout: str) -> dict:
        return self.call("/project/boards", {"checkout": checkout})

    def worktree_ensure(self, project: str, user_id: int, is_owner: bool) -> dict:
        return self.call("/worktree/ensure", {"project": project, "user_id": user_id,
                                              "is_owner": is_owner})

    def run_mark(self, rid: str, active: bool, ttl: float = 60.0) -> None:
        self.call("/runs/mark", {"id": rid, "active": active, "ttl": ttl})

    def git_set_remote(self, checkout: str, url: str) -> str:
        return self.call("/git/set_remote", {"checkout": checkout, "url": url})["url"]
