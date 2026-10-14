"""blpl-wsagent: the control plane's one way into a project's workspace.

Phase 3 of docs/architecture-v2.md. This runs inside the workspace container as
its main process, and listens on a Unix socket in /run/blpl, a directory
bind-mounted from the host and shared with the control plane alone. Through it,
the control plane reads and writes project files, runs git and starts runs,
without mounting the project itself.

It runs in the untrusted container, so it is written to be small and stdlib
only. That lets the workspace image carry it as one file
(/opt/blpl/wsagent/agent.py) beside one more, ``projects.py``. That module is
the backend's own git layer, which is stdlib only too, so status, diff, commit,
pull and push behave exactly as they do when the backend runs them. Its replies
are data to the control plane, never instructions.

**Who may talk to it.** Every request carries ``Authorization: Bearer <token>``.
The token is per start: the control plane derives it from the server key and a
nonce, writes it to /run/blpl/agent.token before starting the container, and
this process reads that file and unlinks it before serving anything. A run
started later, as the same uid in the same container, finds no file. The
process also marks itself non-dumpable (PR_SET_DUMPABLE 0), so a same-uid
process cannot read the token out of /proc/<pid>/mem or environ. Without a
token file it refuses to serve.

**Where it may touch.** Every path is relative to a checkout. The checkout is
an absolute path lying within one of the roots given at start (the project
directory and its worktrees directory), with no symlink in it. Below the
checkout a path is opened one component at a time from the previous
directory's descriptor, each component lstat'ed first and the opened descriptor
checked to be that same inode (``open_at``; O_NOFOLLOW alone does not hold
under gVisor). A symlink anywhere along the way, final
component included, is therefore refused rather than followed. That is the
same rule FilesystemSandbox applies by resolving and containing, made
race-free by never following a link at all. No path may name ``.git``: git is
reached through the git verbs only.

**Writes during a run** are refused (409) while any run is active, except under
``.blpl/``, where the chat's transcripts and the llm-ignore list live and which
no stage reads. A run cannot see its inputs change under it.

Run with:  python agent.py   (configuration from BLPL_AGENT_* variables)
"""

from __future__ import annotations

import ctypes
import hmac
import json
import logging
import os
import re
import shutil
import signal
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit

# The backend modules the agent drives, all stdlib (plus the pipeline's own
# quarantine). In the backend's tree they are the `app` package itself; in the
# workspace image the same files sit in /opt/blpl/wsagent/app beside this one.
from app import projects as _projects  # type: ignore[import-not-found]

logger = logging.getLogger("blpl.wsagent")

VERSION = 1
MAX_BODY = 160 * 1024 * 1024  # an upload's cap plus headroom
RUN_LOG_DIR = Path(os.environ.get("BLPL_AGENT_RUN_LOGS", "/tmp/blpl-runs"))
CANCEL_GRACE_S = 10.0
# How long a held origin lock waits for the control plane's release before
# letting go by itself. Long enough for a database write, short enough that a
# control plane that died mid-hold blocks the next push only briefly.
HOLD_TIMEOUT_S = 30.0


class AgentError(Exception):
    def __init__(self, status: int, message: str, kind: str = "error"):
        super().__init__(message)
        self.status, self.kind = status, kind


# How long a connection may take to send its request. A body that trickles
# in, or never ends, holds one thread for this long and no longer.
REQUEST_TIMEOUT_S = 60.0


def read_body(handler: BaseHTTPRequestHandler, cap: int, *, required: bool) -> bytes:
    """A request body, exactly as long as its Content-Length says, or refused.

    ``int(header)`` accepted ``-1``, and ``rfile.read(-1)`` reads until the
    peer closes: one request could buffer without limit (Codex, PR #20). So
    the length must be digits, within the cap, and present when a body is
    required; a chunked body is refused outright, since nothing here needs
    one. A refusal closes the connection: the unread body would otherwise be
    parsed as the next request. The handler's socket timeout bounds a slow one.
    """
    def refuse(status: int, message: str) -> AgentError:
        handler.close_connection = True
        return AgentError(status, message, "bad_request")

    if handler.headers.get("Transfer-Encoding"):
        raise refuse(411, "a chunked request body is not accepted; send Content-Length")
    raw = handler.headers.get("Content-Length")
    if raw is None:
        if required:
            raise refuse(411, "Content-Length is required")
        return b""
    raw = raw.strip()
    if not raw.isdigit() or not raw.isascii():
        raise refuse(400, "Content-Length must be a non-negative integer")
    n = int(raw)
    if n > cap:
        raise refuse(413, "request body too large")
    try:
        data = handler.rfile.read(n) if n else b""
    except (TimeoutError, OSError):
        raise refuse(408, "the request body did not arrive in time") from None
    if len(data) != n:
        raise refuse(400, "the request body ended early")
    return data


def _forbidden(msg: str) -> AgentError:
    return AgentError(403, msg, "forbidden")


# -- confinement -------------------------------------------------------------------


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def open_at(name: str, dir_fd: int, flags: int, *, want_dir: bool, mode: int = 0o644,
            create: bool = False) -> int:
    """Open name in dir_fd without following a link, whatever the kernel does.

    O_NOFOLLOW alone is not enough. Under gVisor (runsc), open() with
    O_DIRECTORY | O_NOFOLLOW *follows* a symlink: a link to a directory opens
    the directory it names, and a dangling link fails with ENOENT. Linux
    refuses both with ENOTDIR. So the decision is taken from lstat, which both
    report the same way, and the descriptor the open returns must be the
    inode lstat saw. A link swapped in between the two is refused, not
    followed. FileNotFoundError means the name does not exist at all.
    """
    try:
        before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise
        before = None
    if before is not None:
        if stat.S_ISLNK(before.st_mode):
            raise _forbidden(f"{name!r} is a symlink; links are never followed")
        if want_dir != stat.S_ISDIR(before.st_mode):
            raise _forbidden(f"{name!r} is {'not ' if want_dir else ''}a directory")
    fd = os.open(name, flags | os.O_NOFOLLOW | (os.O_DIRECTORY if want_dir else 0),
                 mode, dir_fd=dir_fd)
    try:
        after = os.fstat(fd)
        now = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (before is not None and not _same(before, after)) or not _same(now, after):
            raise _forbidden(f"{name!r} changed while it was being opened")
    except BaseException:
        os.close(fd)
        raise
    return fd


class Confined:
    def __init__(self, roots: list[str]):
        self.roots = [os.path.realpath(r) for r in roots if r]

    def checkout(self, path: Any) -> str:
        if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
            raise _forbidden("checkout must be an absolute path")
        norm = os.path.normpath(path)
        # realpath == normpath: no component of the checkout path is a link.
        if os.path.realpath(norm) != norm:
            raise _forbidden("checkout path contains a symlink")
        if not any(norm == r or norm.startswith(r + "/") for r in self.roots):
            raise _forbidden("checkout is outside this workspace's projects")
        if not os.path.isdir(norm):
            raise AgentError(404, "no such checkout", "notfound")
        return norm

    @staticmethod
    def parts(rel: Any, *, allow_empty: bool = False) -> list[str]:
        if not isinstance(rel, str) or "\0" in rel or "\\" in rel or rel.startswith("/"):
            raise _forbidden("path must be relative")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts and not allow_empty:
            raise _forbidden("empty path")
        for p in parts:
            if p == "..":
                raise _forbidden("path may not contain ..")
            if p == ".git":
                raise _forbidden("the git directory is reached through the git verbs only")
        return parts

    @contextmanager
    def dir_fd(self, checkout: str, parts: list[str], *, create: bool = False) -> Iterator[int]:
        """A descriptor for checkout/parts[0]/.../parts[-1], following no link."""
        fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for p in parts:
                try:
                    nfd = open_at(p, fd, os.O_RDONLY, want_dir=True)
                except FileNotFoundError:
                    if not create:
                        raise AgentError(404, "no such directory", "notfound") from None
                    os.mkdir(p, 0o755, dir_fd=fd)
                    nfd = open_at(p, fd, os.O_RDONLY, want_dir=True)
                except AgentError:
                    raise
                except OSError as e:
                    raise _forbidden(f"{p!r} is not a directory this agent may enter ({e.strerror})") from None
                os.close(fd)
                fd = nfd
            yield fd
        finally:
            os.close(fd)


# -- runs ----------------------------------------------------------------------------


@dataclass
class _Run:
    id: str
    proc: subprocess.Popen
    log: Path
    exit_code: int | None = None


class Runs:
    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}
        self._marks: dict[str, float] = {}
        self._lock = threading.Lock()
        RUN_LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    def active(self) -> int:
        now = time.monotonic()
        with self._lock:
            own = sum(1 for r in self._runs.values() if r.proc.poll() is None)
            self._marks = {k: t for k, t in self._marks.items() if t > now}
            return own + len(self._marks)

    def mark(self, rid: str, active: bool, ttl: float) -> None:
        """A run in a container of its own (phase 4) counts as active here too,
        so writes wait for it exactly as for a run this agent started. The mark
        expires unless renewed, so a control plane that died mid-run cannot
        leave the workspace refusing writes for ever."""
        with self._lock:
            if active:
                self._marks[rid] = time.monotonic() + max(1.0, min(ttl, 600.0))
            else:
                self._marks.pop(rid, None)

    def start(self, argv: list[str], env: dict[str, str], cwd: str,
              rid: str | None = None) -> str:
        """Start a run, or, for an id already known, return it unstarted.

        The id is the caller's (the database run's), so a worker that retries
        a reclaimed run after the first worker died finds the process the first
        one started and follows it, instead of starting a second copy on the
        same checkout and paying for its model calls twice (Codex, PR #20)."""
        if rid is None:
            rid = uuid.uuid4().hex[:16]
        elif not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", rid):
            raise AgentError(400, "invalid run id")
        with self._lock:
            if rid in self._runs:
                return rid
        log = RUN_LOG_DIR / f"{rid}.log"
        base = {k: v for k, v in os.environ.items() if not k.startswith("BLPL_AGENT_")}
        # Readers in a run never take git's optional locks, so `git status` in
        # provenance cannot rewrite an index under the run. That was the live
        # issue that wanted .git read-only.
        base["GIT_OPTIONAL_LOCKS"] = "0"
        with open(log, "wb") as fh:
            proc = subprocess.Popen(
                argv, cwd=cwd, env={**base, **env}, stdin=subprocess.DEVNULL,
                stdout=fh, stderr=subprocess.STDOUT,
                # Its own session and process group, so cancel reaches every
                # process the run started, not just the first.
                start_new_session=True,
            )
        with self._lock:
            self._runs[rid] = _Run(rid, proc, log)
        return rid

    def get(self, rid: str) -> _Run:
        with self._lock:
            run = self._runs.get(rid)
        if run is None:
            raise AgentError(404, "no such run", "notfound")
        run.exit_code = run.proc.poll()
        return run

    def cancel(self, rid: str) -> None:
        run = self.get(rid)
        if run.proc.poll() is not None:
            return
        try:
            os.killpg(run.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

        def reap() -> None:
            deadline = time.monotonic() + CANCEL_GRACE_S
            while time.monotonic() < deadline:
                if run.proc.poll() is not None:
                    break
                time.sleep(0.1)
            try:
                os.killpg(run.proc.pid, signal.SIGKILL)  # whatever is left of the group
            except ProcessLookupError:
                pass

        threading.Thread(target=reap, daemon=True).start()


# -- the agent ------------------------------------------------------------------------


class _Identity:
    """Duck-types gitidentity.GitIdentity, which projects.py takes."""

    def __init__(self, name: str, email: str):
        self.name, self.email = name, email

    def env(self) -> dict[str, str]:
        return {"GIT_AUTHOR_NAME": self.name, "GIT_AUTHOR_EMAIL": self.email,
                "GIT_COMMITTER_NAME": self.name, "GIT_COMMITTER_EMAIL": self.email}


def _set_ignored(checkout: str, rel: str, by: str) -> None:
    """Add rel to .blpl/llm-ignore.json, in llm_ignore.py's format."""
    target = Path(checkout) / ".blpl" / "llm-ignore.json"
    try:
        rows = json.loads(target.read_text()).get("files", [])
    except (FileNotFoundError, ValueError):
        rows = []
    if any(r.get("path") == rel for r in rows):
        return
    rows.append({"path": rel, "added_by": by,
                 "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    rows.sort(key=lambda r: r["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps({"schema_version": 1, "files": rows}, indent=2) + "\n")
    tmp.replace(target)


# Repository-controlled settings that run a program: a hook on commit, push or
# checkout, and an fsmonitor command on status. None of them may run in the
# agent's git, whatever the repository's config says, so they are overridden
# on every command (GIT_CONFIG_PARAMETERS is what `git -c` sets).
_GIT_HARDENING = ("'core.hooksPath'='/dev/null'", "'core.fsmonitor'='false'",
                  # GIT_SSH_COMMAND, set for a leased ssh key, still wins over
                  # this; without one, plain ssh rather than whatever the
                  # repository's core.sshCommand names.
                  "'core.sshCommand'='ssh'")


def _connect_proxy(environ) -> tuple[str, int] | None:
    """The workspace's egress proxy, from the HTTPS_PROXY the driver set."""
    from urllib.parse import urlsplit

    raw = (environ.get("HTTPS_PROXY") or environ.get("https_proxy")
           or environ.get("HTTP_PROXY") or environ.get("http_proxy") or "")
    if not raw:
        return None
    parts = urlsplit(raw if "://" in raw else "http://" + raw)
    if not parts.hostname:
        return None
    return parts.hostname, parts.port or 3128


def ssh_command(key_path: str | None, environ) -> str:
    """GIT_SSH_COMMAND for one network git command in the workspace.

    A workspace sits on an internal network whose one way out is the egress
    proxy, and OpenSSH ignores HTTP_PROXY. So ssh is told to reach the
    remote through the proxy itself: CONNECT host:22, which Squid allows for
    the git hosts only (app/egress). Squid also wants the operation's proxy
    credential, which OpenBSD netcat can only prompt for, so the proxy
    command is this file's own ``--connect`` mode: it reads the credential
    from the HTTPS_PROXY it inherits through git and ssh, never from argv.

    Always set for a pull or push, with or without a leased key. GIT_SSH_COMMAND
    outranks core.sshCommand, so whatever ssh wrapper the repository's config
    names never runs."""
    import shlex

    parts = ["ssh"]
    if key_path:
        parts += ["-i", shlex.quote(key_path), "-o", "IdentitiesOnly=yes"]
    parts += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    proxy = _connect_proxy(environ)
    if proxy is not None:
        host, port = proxy
        parts += ["-o", shlex.quote(f"ProxyCommand=python {AGENT_PATH} --connect %h %p")]
    return " ".join(parts)


# Where the workspace image puts this file (app/workspace/Dockerfile).
AGENT_PATH = "/opt/blpl/wsagent/agent.py"
_PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")


def connect_main(host: str, port: str, environ=os.environ) -> int:
    """ssh's ProxyCommand: CONNECT host:port through the egress proxy,
    authenticating with the credential in HTTPS_PROXY, then relay stdin and
    stdout to the tunnel until either side closes."""
    import base64
    import selectors
    import socket
    from urllib.parse import unquote, urlsplit

    raw = next((environ[v] for v in _PROXY_VARS if environ.get(v)), "")
    parts = urlsplit(raw if "://" in raw else "http://" + raw)
    if not parts.hostname:
        print("blpl connect: no proxy configured", file=sys.stderr)
        return 2
    s = socket.create_connection((parts.hostname, parts.port or 3128), timeout=30)
    head = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
    if parts.username:
        pair = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
        head.append("Proxy-Authorization: Basic " + base64.b64encode(pair).decode())
    s.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
    reply = b""
    while b"\r\n\r\n" not in reply:
        chunk = s.recv(4096)
        if not chunk or len(reply) > 16384:
            print("blpl connect: the proxy closed the connection", file=sys.stderr)
            return 1
        reply += chunk
    status, _, rest = reply.partition(b"\r\n\r\n")
    code = status.split(b" ", 2)[1:2]
    if code != [b"200"]:
        print(f"blpl connect: the proxy refused {host}:{port}: "
              f"{status.splitlines()[0].decode('latin-1')}", file=sys.stderr)
        return 1
    s.settimeout(None)
    out = sys.stdout.buffer
    if rest:
        out.write(rest)
        out.flush()
    sel = selectors.DefaultSelector()
    sel.register(sys.stdin.buffer, selectors.EVENT_READ, "in")
    sel.register(s, selectors.EVENT_READ, "net")
    while True:
        for key, _ in sel.select():
            if key.data == "in":
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    try:
                        s.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    sel.unregister(sys.stdin.buffer)
                    continue
                s.sendall(data)
            else:
                data = s.recv(65536)
                if not data:
                    return 0
                out.write(data)
                out.flush()


def harden_git_environment(environ) -> None:
    """Put the hardening into environ's GIT_CONFIG_PARAMETERS, keeping
    anything already there. Called once by main() for the agent's process, so
    every git it or projects.py starts inherits it."""
    have = environ.get("GIT_CONFIG_PARAMETERS", "")
    extra = " ".join(p for p in _GIT_HARDENING if p not in have)
    if extra:
        environ["GIT_CONFIG_PARAMETERS"] = (have + " " + extra).strip()


def _below_blpl(parts: list[str]) -> bool:
    return bool(parts) and parts[0] == ".blpl"


class Agent:
    def __init__(self, token: str, roots: list[str], *, projects_root: str | None = None,
                 allow_runs: bool = False):
        if not token:
            raise ValueError("no token")
        self.token = token.encode()
        self.fs = Confined(roots)
        # The directory projects and .worktrees sit in. In a workspace the
        # roots are <it>/<P> and <it>/.worktrees/<P>.
        self._projects_root = projects_root or (
            os.path.dirname(self.fs.roots[0]) if self.fs.roots else "/")
        self.runs = Runs()
        # Off unless asked for, and nothing in a deployment asks. A process
        # this agent starts shares its container, its uid and the read-write
        # /run/blpl that holds the socket: it could rename agent.sock and
        # listen in its place, and the control plane would hand its bearer
        # token, and with pushes a leased credential, to the impostor. The
        # agent cannot run a stage as another uid (no CAP_SETUID, and
        # no-new-privileges). So with the agent in use, the worker runs stages
        # in run containers of their own, which do not mount /run/blpl
        # (Codex, PR #20).
        self.allow_runs = allow_runs
        self.git = _projects.Projects(Path(self.fs.roots[0]) if self.fs.roots else Path("/"))
        self._last = time.monotonic()
        # Requests being served now. Reported by /health, so the idle sweep
        # never takes a workspace for idle while one is in progress, however
        # long ago it started: a push of minutes touched only at its start
        # aged past the idle limit mid-write (Codex, PR #20).
        self._active = 0
        self._active_lock = threading.Lock()
        self._holds: dict[str, threading.Event] = {}
        self._holds_lock = threading.Lock()

    def authorised(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip().encode(), self.token)

    def touch(self) -> None:
        self._last = time.monotonic()

    def begin(self) -> None:
        with self._active_lock:
            self._active += 1
        self.touch()

    def end(self) -> None:
        with self._active_lock:
            self._active -= 1
        self.touch()  # its end is activity too: the idle clock starts here

    def _no_run(self, parts: list[str]) -> None:
        if not _below_blpl(parts) and self.runs.active():
            raise AgentError(409, "a run is in progress in this workspace; "
                                  "try again when it has finished", "busy")

    # -- files ---------------------------------------------------------------

    def fs_list(self, b: dict) -> dict:
        checkout = self.fs.checkout(b.get("checkout"))
        parts = self.fs.parts(b.get("path", ""), allow_empty=True)
        skip = set(b.get("skip") or [])
        max_depth = int(b.get("max_depth", 12))
        out: list[dict] = []

        def walk(fd: int, prefix: str, depth: int) -> bool:
            if depth > max_depth:
                with os.scandir(fd) as it:
                    return next(it, None) is None
            complete = True
            with os.scandir(fd) as it:
                entries = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False),
                                                    e.name.lower()))
            for e in entries:
                if e.name in skip or e.name == ".git":
                    complete = False
                    continue
                rel = f"{prefix}{e.name}"
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    complete = False
                    continue
                if stat.S_ISDIR(st.st_mode):
                    node = {"path": rel, "name": e.name, "dir": True}
                    out.append(node)
                    try:
                        sub = open_at(e.name, fd, os.O_RDONLY, want_dir=True)
                    except (OSError, AgentError):
                        node["incomplete"] = True
                        complete = False
                        continue
                    try:
                        if not walk(sub, rel + "/", depth + 1):
                            node["incomplete"] = True
                            complete = False
                    finally:
                        os.close(sub)
                else:
                    out.append({"path": rel, "name": e.name, "dir": False,
                                "bytes": st.st_size, "mtime": st.st_mtime,
                                "btime": getattr(st, "st_birthtime", None),
                                "symlink": stat.S_ISLNK(st.st_mode)})
            return complete

        with self.fs.dir_fd(checkout, parts) as fd:
            complete = walk(fd, "/".join(parts) + "/" if parts else "", 0)
        return {"entries": out, "complete": complete}

    def fs_stat(self, b: dict) -> dict:
        checkout = self.fs.checkout(b.get("checkout"))
        parts = self.fs.parts(b.get("path"))
        try:
            with self.fs.dir_fd(checkout, parts[:-1]) as fd:
                st = os.lstat(parts[-1], dir_fd=fd)
        except (AgentError, FileNotFoundError):
            return {"exists": False}
        return {"exists": True, "dir": stat.S_ISDIR(st.st_mode),
                "symlink": stat.S_ISLNK(st.st_mode), "bytes": st.st_size,
                "mtime": st.st_mtime}

    def fs_read(self, b: dict) -> bytes:
        checkout = self.fs.checkout(b.get("checkout"))
        parts = self.fs.parts(b.get("path"))
        cap = int(b.get("max_bytes", MAX_BODY))
        with self.fs.dir_fd(checkout, parts[:-1]) as dfd:
            try:
                fd = open_at(parts[-1], dfd, os.O_RDONLY, want_dir=False)
            except FileNotFoundError:
                raise AgentError(404, "no such file", "notfound") from None
            except AgentError:
                raise
            except OSError as e:
                raise _forbidden(f"cannot open {parts[-1]!r}: {e.strerror}") from None
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise _forbidden("not a regular file")
            if st.st_size > cap:
                raise AgentError(413, f"file is {st.st_size} bytes, over {cap}", "too_large")
            return fh.read()

    def fs_write(self, q: dict, data: bytes) -> dict:
        checkout = self.fs.checkout(q.get("checkout"))
        parts = self.fs.parts(q.get("path"))
        self._no_run(parts)
        with self.fs.dir_fd(checkout, parts[:-1], create=q.get("mkdirs") == "1") as dfd:
            try:
                # No O_TRUNC: truncating happens only once the descriptor is
                # known to be the file itself, never something a link named.
                fd = open_at(parts[-1], dfd, os.O_WRONLY | os.O_CREAT, want_dir=False,
                             create=True)
            except AgentError:
                raise
            except OSError as e:
                raise _forbidden(f"cannot write {parts[-1]!r}: {e.strerror}") from None
        with os.fdopen(fd, "wb") as fh:
            os.ftruncate(fh.fileno(), 0)
            fh.write(data)
        return {"bytes": len(data)}

    def fs_mkdir(self, b: dict) -> dict:
        checkout = self.fs.checkout(b.get("checkout"))
        parts = self.fs.parts(b.get("path"))
        self._no_run(parts)
        with self.fs.dir_fd(checkout, parts[:-1], create=True) as dfd:
            try:
                os.mkdir(parts[-1], 0o755, dir_fd=dfd)
            except FileExistsError:
                raise AgentError(409, "already exists", "exists") from None
        return {"ok": True}

    def fs_delete(self, b: dict) -> dict:
        checkout = self.fs.checkout(b.get("checkout"))
        parts = self.fs.parts(b.get("path"))
        self._no_run(parts)
        with self.fs.dir_fd(checkout, parts[:-1]) as dfd:
            try:
                st = os.lstat(parts[-1], dir_fd=dfd)
            except FileNotFoundError:
                raise AgentError(404, "no such file", "notfound") from None
            if stat.S_ISDIR(st.st_mode):
                if not b.get("recursive"):
                    raise AgentError(409, "is a directory", "is_dir")
                # dir_fd rmtree is the fd-based, symlink-safe variant: a link
                # inside the tree is removed, never followed.
                shutil.rmtree(parts[-1], dir_fd=dfd)
            else:
                os.unlink(parts[-1], dir_fd=dfd)
        return {"ok": True}

    def fs_rename(self, b: dict) -> dict:
        checkout = self.fs.checkout(b.get("checkout"))
        src = self.fs.parts(b.get("src"))
        dst = self.fs.parts(b.get("dst"))
        self._no_run(src)
        self._no_run(dst)
        with self.fs.dir_fd(checkout, src[:-1]) as sfd, \
                self.fs.dir_fd(checkout, dst[:-1], create=bool(b.get("mkdirs"))) as dfd:
            try:
                os.lstat(src[-1], dir_fd=sfd)
            except FileNotFoundError:
                raise AgentError(404, "no such file", "notfound") from None
            try:
                os.lstat(dst[-1], dir_fd=dfd)
                raise AgentError(409, "destination exists", "exists")
            except FileNotFoundError:
                pass
            os.rename(src[-1], dst[-1], src_dir_fd=sfd, dst_dir_fd=dfd)
        return {"ok": True}

    # -- the file manager's moves and deletes ---------------------------------
    #
    # What main.delete_path and main.move_path do after their lexical checks,
    # here, beside the files: the quarantine ledger and the LLM-ignore list
    # are read and rewritten where they live, under llm_ignore's own lock.
    # The order of every step is the route's, for the reasons its comments
    # give. The control plane still decides who may and which names are legal.

    def _bookkeeping_dirs_are_real(self, checkout: str) -> None:
        for name in (".blpl", "retrieved", "datasheets", "references"):
            try:
                if stat.S_ISLNK(os.lstat(os.path.join(checkout, name)).st_mode):
                    raise _forbidden(f"{name}/ is a symlink in this checkout; refusing to use it")
            except FileNotFoundError:
                pass

    def _link_leaves(self, checkout: str, parts: list[str], dfd: int) -> bool:
        """Whether parts names a link pointing outside the checkout.

        Such a link is never followed here, so removing or moving it cannot
        touch what it names. It is refused anyway, as the backend's sandbox
        refuses it, because a link out of the project is not something the
        file manager offers to rearrange."""
        try:
            target = os.readlink(parts[-1], dir_fd=dfd)
        except OSError:
            return False  # not a link
        if target.startswith("/"):
            dest = os.path.normpath(target)
        else:
            dest = os.path.normpath(os.path.join(checkout, *parts[:-1], target))
        return not (dest == checkout or dest.startswith(checkout + "/"))

    def managed_delete(self, b: dict) -> dict:
        from app import llm_ignore

        checkout = self.fs.checkout(b.get("checkout"))
        self._verify_git(checkout)  # llm_ignore's lock follows the gitfile
        parts = self.fs.parts(b.get("path"))
        rel = "/".join(parts)
        self._no_run(parts)
        self._bookkeeping_dirs_are_real(checkout)
        with self.fs.dir_fd(checkout, parts[:-1]) as dfd:
            try:
                st = os.lstat(parts[-1], dir_fd=dfd)
            except FileNotFoundError:
                raise AgentError(404, f"no file or folder {rel!r}", "notfound") from None
            if self._link_leaves(checkout, parts, dfd):
                raise _forbidden(f"{rel!r} is a link out of the project")
            recorded = llm_ignore.released_under(Path(checkout), rel)
            if recorded:
                raise AgentError(409, (
                    f"{', '.join(recorded[:3])}{' and others' if len(recorded) > 3 else ''} "
                    "is recorded in the quarantine ledger as released at its current path; "
                    "deleting it would leave the ledger naming a file that is not there"),
                    "recorded")
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(parts[-1], dir_fd=dfd)
                kind = "folder"
            else:
                os.unlink(parts[-1], dir_fd=dfd)
                kind = "file"
        updated = True
        try:
            llm_ignore.drop_entries(Path(checkout), rel)
        except OSError:
            updated = False
        return {"kind": kind, "ignore_list_updated": updated}

    def managed_move(self, b: dict) -> dict:
        from app import llm_ignore

        checkout = self.fs.checkout(b.get("checkout"))
        self._verify_git(checkout)  # llm_ignore's lock follows the gitfile
        src = self.fs.parts(b.get("src"))
        dst = self.fs.parts(b.get("dst"))
        src_rel, dst_rel = "/".join(src), "/".join(dst)
        self._no_run(src)
        self._no_run(dst)
        self._bookkeeping_dirs_are_real(checkout)
        with self.fs.dir_fd(checkout, src[:-1]) as sfd:
            try:
                sst = os.lstat(src[-1], dir_fd=sfd)
            except FileNotFoundError:
                raise AgentError(404, f"no file or folder {src_rel!r}", "notfound") from None
            if self._link_leaves(checkout, src, sfd):
                raise _forbidden(f"{src_rel!r} is a link out of the project")
        try:
            with self.fs.dir_fd(checkout, dst[:-1]) as dfd:
                os.lstat(dst[-1], dir_fd=dfd)
            raise AgentError(409, f"{dst_rel} already exists", "exists")
        except (FileNotFoundError, AgentError) as e:
            if isinstance(e, AgentError) and e.status != 404:
                raise
        if stat.S_ISDIR(sst.st_mode) and (dst_rel == src_rel or dst_rel.startswith(src_rel + "/")):
            raise AgentError(400, "a folder cannot be moved inside itself")
        recorded = llm_ignore.released_under(Path(checkout), src_rel)
        if recorded:
            raise AgentError(409, (
                f"{', '.join(recorded[:3])}{' and others' if len(recorded) > 3 else ''} "
                "is recorded in the quarantine ledger at its current path; moving it would "
                "leave the ledger naming a file that is not there"), "recorded")
        try:
            before = llm_ignore.cover_move(Path(checkout), src_rel, dst_rel)
        except OSError as e:
            raise AgentError(500, f"{src_rel} was not moved: the LLM-ignore list could not "
                                  f"be updated ({e})", "not_moved") from None
        try:
            with self.fs.dir_fd(checkout, src[:-1]) as sfd, \
                    self.fs.dir_fd(checkout, dst[:-1], create=True) as dfd:
                os.rename(src[-1], dst[-1], src_dir_fd=sfd, dst_dir_fd=dfd)
        except (OSError, AgentError) as e:
            if before is not None:
                try:
                    llm_ignore.restore(Path(checkout), before)
                except OSError:
                    pass
            if isinstance(e, AgentError):
                raise
            raise AgentError(500, f"{src_rel} was not moved: {e}", "not_moved") from None
        updated = True
        if before is not None:
            try:
                llm_ignore.finish_move(Path(checkout), before)
            except OSError:
                updated = False
        return {"ignore_list_updated": updated}

    # -- the LLM-ignore list, the quarantine ledger, the board manifest -------

    def ignore_entries(self, b: dict) -> dict:
        from app import llm_ignore

        checkout = self.fs.checkout(b.get("checkout"))
        self._verify_git(checkout)  # llm_ignore's lock follows the gitfile
        self._bookkeeping_dirs_are_real(checkout)
        return {"files": llm_ignore.entries(Path(checkout))}

    def ignore_set(self, b: dict) -> dict:
        from app import llm_ignore

        checkout = self.fs.checkout(b.get("checkout"))
        self._verify_git(checkout)  # llm_ignore's lock follows the gitfile
        parts = self.fs.parts(b.get("path"))
        self._no_run([".blpl"])
        self._bookkeeping_dirs_are_real(checkout)
        with self.fs.dir_fd(checkout, parts[:-1]) as dfd:
            try:
                fd = open_at(parts[-1], dfd, os.O_RDONLY, want_dir=False)
                os.close(fd)
            except (FileNotFoundError, AgentError):
                raise AgentError(404, f"no file {b.get('path')!r} in this project",
                                 "notfound") from None
        rel = "/".join(parts)
        changed = llm_ignore.set_ignored(Path(checkout), rel, bool(b.get("ignored")),
                                         by=str(b.get("by", "")))
        return {"path": rel, "changed": changed}

    def quarantine_view(self, b: dict) -> dict:
        from blpl.core import quarantine

        checkout = self.fs.checkout(b.get("checkout"))
        self._bookkeeping_dirs_are_real(checkout)
        return {"summary": quarantine.summary(Path(checkout)),
                "entries": list(reversed(quarantine.ledger(Path(checkout))))[:500]}

    def project_boards(self, b: dict) -> dict:
        from blpl.core import project_manifest

        checkout = self.fs.checkout(b.get("checkout"))
        try:
            man = project_manifest.discover(Path(checkout))
        except project_manifest.ManifestError as e:
            raise AgentError(400, str(e), "manifest") from None
        return {"implicit": man.implicit, "boards": [bd.name for bd in man.boards]}

    def worktree_ensure(self, b: dict) -> dict:
        """worktrees.ensure for a member, in the project's own storage: the
        owner's checkout is the repository, members get .worktrees/<P>/u<id>."""
        from app import worktrees

        project = str(b.get("project", ""))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", project):
            raise _forbidden("invalid project name")
        repo = self.fs.checkout(os.path.join(self.projects_root(), project))
        d = worktrees.ensure(Path(os.path.dirname(repo)), project, int(b.get("user_id", 0)),
                             bool(b.get("is_owner")))
        return {"path": str(d), "exists": d.is_dir()}

    def worktree_remove(self, b: dict) -> dict:
        """worktrees.remove, for a member who lost access: their checkout
        goes, their branch stays. Here because without the project mount the
        control plane sees no checkout to remove (Codex, PR #20)."""
        from app import worktrees

        project = str(b.get("project", ""))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", project):
            raise _forbidden("invalid project name")
        repo = self.fs.checkout(os.path.join(self.projects_root(), project))
        self._no_run(["(worktree)"])
        return {"removed": worktrees.remove(Path(os.path.dirname(repo)), project,
                                            int(b.get("user_id", 0)))}

    def projects_root(self) -> str:
        return self._projects_root

    # -- quarantine -----------------------------------------------------------

    # The directories quarantine writes with ordinary paths. Each must be a
    # real directory (or absent) in the checkout: a committed link named
    # `datasheets` would otherwise carry an upload wherever it points.
    _QUARANTINE_DIRS = ("datasheets", "references", "retrieved", ".blpl")

    def quarantine_accept(self, q: dict, data: bytes) -> dict:
        checkout = self.fs.checkout(q.get("checkout"))
        self._verify_git(checkout)  # llm_ignore's lock follows the gitfile
        self._no_run(["datasheets"])
        for name in self._QUARANTINE_DIRS:
            try:
                if stat.S_ISLNK(os.lstat(os.path.join(checkout, name)).st_mode):
                    raise _forbidden(f"{name}/ is a symlink in this checkout; refusing to write through it")
            except FileNotFoundError:
                pass
        from blpl.core import quarantine  # the pipeline's own; present in the workspace image

        by = q.get("uploaded_by", "")
        try:
            rec = quarantine.accept_upload(
                data, checkout, original_name=q.get("original_name", "upload"),
                kind=q.get("kind", ""), uploaded_by=by, mpn=q.get("mpn", ""),
                llm_ignore=q.get("llm_ignore") == "True",
                # The control plane's policy, per request, as well as this
                # container's environment: a workspace created before the
                # policy was forwarded keeps the environment it started with,
                # and must still hold what nothing scanned (Codex, PR #20).
                insist_on_scan=q.get("require_scan") == "1",
            )
        except ValueError as e:
            raise AgentError(400, str(e)) from None
        path = (f"{rec.released_to}/{rec.released_as}"
                if rec.state == quarantine.RELEASED else None)
        if path and rec.llm_ignore:
            _set_ignored(checkout, path, by)
        return {"state": rec.state, "kind": rec.kind, "path": path, "reasons": rec.reasons,
                "inspection": (rec.inspection or {}).get("state", ""),
                "scan": (rec.scan or {}).get("state", ""), "llm_ignore": rec.llm_ignore}

    # -- git -------------------------------------------------------------------

    def _checkout_path(self, b: dict) -> Path:
        checkout = self.fs.checkout(b.get("checkout"))
        self._verify_git(checkout)
        return Path(checkout)

    def _verify_git(self, checkout: str) -> None:
        """Refuse to run git in a checkout whose .git is not what the app made.

        The owner's checkout, <projects>/<P>, has a .git directory. A member's,
        <projects>/.worktrees/<P>/<name>, has a .git file pointing at
        <projects>/<P>/.git/worktrees/<entry>, and that entry's own `gitdir`
        file points back at it. Anything else (a link, a directory in a
        worktree, a pointer somewhere a stage could have written hooks to) is
        refused, not followed (Codex, PR #20). Run containers bind every gitfile
        read-only, so this is the second line, not the first."""
        root = self._projects_root.rstrip("/")
        dot = os.path.join(checkout, ".git")
        try:
            st = os.lstat(dot)
        except FileNotFoundError:
            return  # not a repository; git will say so itself
        rel = checkout[len(root) + 1:] if checkout.startswith(root + "/") else None
        parts = rel.split("/") if rel else []
        if stat.S_ISDIR(st.st_mode):
            if len(parts) == 1:
                return
            raise _forbidden("a .git directory inside a member's worktree is not the app's")
        if not stat.S_ISREG(st.st_mode) or len(parts) != 3 or parts[0] != ".worktrees":
            raise _forbidden(".git here is neither the project's repository nor a worktree's pointer")
        project = parts[1]
        with open(dot, encoding="utf-8", errors="replace") as fh:
            line = fh.read(4096).strip()
        if not line.startswith("gitdir:"):
            raise _forbidden("this worktree's .git file is not a gitdir pointer")
        target = line[len("gitdir:"):].strip()
        target = os.path.normpath(target if target.startswith("/") else os.path.join(checkout, target))
        expected_parent = f"{root}/{project}/.git/worktrees"
        if (os.path.dirname(target) != expected_parent
                or os.path.realpath(target) != target):
            raise _forbidden(f"this worktree's .git points at {target}, not into "
                             f"{expected_parent}; refusing to use it")
        try:
            with open(os.path.join(target, "gitdir"), encoding="utf-8") as fh:
                back = os.path.normpath(fh.read(4096).strip())
        except OSError:
            raise _forbidden("this worktree's git directory has no back-pointer") from None
        if back != os.path.normpath(dot):
            raise _forbidden("this worktree's git directory belongs to another checkout")

    def git_status(self, b: dict) -> dict:
        return asdict(self.git.status("", checkout=self._checkout_path(b)))

    def git_diff(self, b: dict) -> dict:
        return self.git.diff("", checkout=self._checkout_path(b))

    def git_remote_of(self, b: dict) -> dict:
        return {"url": self.git.remote_of("", operation=b.get("operation", ""),
                                          checkout=self._checkout_path(b))}

    def git_commit(self, b: dict) -> dict:
        d = self._checkout_path(b)
        self._no_run(["(commit)"])
        ident = b.get("identity")
        identity = _Identity(ident["name"], ident["email"]) if ident else None
        return {"output": self.git.commit_checkout(d, str(b.get("message", "")),
                                                    identity=identity)}

    def git_recoverable(self, b: dict) -> dict:
        d = str(self._checkout_path(b))

        def names(*args: str) -> set[str]:
            out = subprocess.run(["git", "--no-optional-locks", "-C", d, *args],
                                 capture_output=True, timeout=20, check=True).stdout
            return {p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p}
        try:
            paths = names("ls-tree", "-r", "-z", "--name-only", "HEAD") - \
                names("diff", "--name-only", "-z", "--no-renames", "HEAD", "--")
        except (OSError, subprocess.SubprocessError):
            return {"paths": None}
        return {"paths": sorted(paths)}

    @contextmanager
    def _credential(self, b: dict) -> Iterator[dict[str, str] | None]:
        """The operation's credentials, for this one git command, then gone:
        the remote's (env, ssh_key), and the egress proxy's (proxy), which the
        control plane issues for this one operation."""
        env = b.get("env")
        key = b.get("ssh_key")
        proxy = {k: str(v) for k, v in (b.get("proxy") or {}).items() if k in _PROXY_VARS}
        if not env:
            # No credential: still clear any credential.helper the repository
            # configures (a program it would run), as a leased credential's
            # own environment does. GIT_CONFIG_PARAMETERS cannot do this: it
            # is read after GIT_CONFIG_COUNT and would clear a lease's helper.
            env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
                   "GIT_CONFIG_VALUE_0": "", "GIT_TERMINAL_PROMPT": "0"}
        env = {**env, **proxy}
        seen = {**os.environ, **proxy}
        if key is None:
            # ssh remotes go through the egress proxy even without a key
            # (a public repository, or a host key the operator mounted).
            yield {**env, "GIT_SSH_COMMAND": ssh_command(None, seen)}
            return
        fd, path = tempfile.mkstemp(prefix="blpl-git-", suffix=".key")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(key if key.endswith("\n") else key + "\n")
            yield {**env, "GIT_SSH_COMMAND": ssh_command(path, seen)}
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def git_pull(self, b: dict) -> dict:
        d = self._checkout_path(b)
        self._no_run(["(pull)"])
        with self._credential(b) as env:
            return {"output": self.git.pull("", env=env, checkout=d, url=b.get("url"))}

    def git_push(self, b: dict) -> dict:
        d = self._checkout_path(b)
        if b.get("hold"):
            def op() -> str:
                with self._credential(b) as env:
                    # What Projects.push does inside its lock; the lock is
                    # taken by _held, which keeps it past the answer.
                    return self.git._push_locked(d, b.get("url"), env)
            return self._held(d, op)
        with self._credential(b) as env:
            return {"output": self.git.push("", env=env, checkout=d, url=b.get("url"))}

    # -- the origin lock, held for the control plane ---------------------------
    #
    # Project setup records a push's outcome while the origin lock is still
    # held (Projects.push's `after`), so overlapping pushes record in the order
    # they ran. The lock lives in the project's git directory, where the agent
    # runs git, and the record lives in the control plane's database. So a held
    # push does its work under the lock, answers with the outcome and a lease,
    # and keeps the lock until the control plane has recorded and released it
    # (or HOLD_TIMEOUT_S passes). /git/hold does the same with no work, for
    # outcomes that never reached a push (Projects.under_remote_lock).

    def _held(self, d: Path, op) -> dict:
        lease = uuid.uuid4().hex
        ready, release = threading.Event(), threading.Event()
        res: dict[str, Any] = {}

        def under_lock() -> None:
            try:
                res["output"] = op()
            except _projects.ProjectError as e:
                res["error"] = str(e)
            ready.set()
            if not release.wait(HOLD_TIMEOUT_S):
                logger.warning("origin lock lease %s was not released; letting go", lease)

        def target() -> None:
            try:
                self.git.under_remote_lock(d, under_lock)
            except _projects.ProjectError as e:  # the lock itself could not be had
                if not ready.is_set():
                    res["error"], res["unlocked"] = str(e), True
            except Exception as e:  # noqa: BLE001
                if not ready.is_set():
                    res["error"], res["unlocked"] = f"{type(e).__name__}: {e}", True
            finally:
                with self._holds_lock:
                    self._holds.pop(lease, None)
                ready.set()

        with self._holds_lock:
            self._holds[lease] = release
        threading.Thread(target=target, daemon=True, name="wsagent-hold").start()
        ready.wait()
        return {"output": res.get("output"), "error": res.get("error"),
                "lease": None if res.get("unlocked") else lease}

    def git_set_remote(self, b: dict) -> dict:
        """Projects.set_remote, under the origin lock in this sandbox. With
        every lock-taker here, the lock means the same under gVisor, whose file
        locks are not visible outside the sandbox."""
        return {"url": self.git.set_remote(self._checkout_path(b), str(b.get("url", "")))}

    def git_hold(self, b: dict) -> dict:
        return self._held(self._checkout_path(b), lambda: None)

    def git_release(self, b: dict) -> dict:
        with self._holds_lock:
            ev = self._holds.get(str(b.get("lease", "")))
        if ev is not None:
            ev.set()
        return {"released": ev is not None}

    # -- runs ------------------------------------------------------------------

    def run_start(self, b: dict) -> dict:
        if not self.allow_runs:
            raise _forbidden("this agent does not start runs; runs go in run containers")
        argv = b.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise AgentError(400, "argv must be a non-empty list of strings")
        env = b.get("env") or {}
        cwd = self.fs.checkout(b.get("cwd")) if b.get("cwd") else "/tmp"
        rid = b.get("id")
        return {"id": self.runs.start(argv, {str(k): str(v) for k, v in env.items()}, cwd,
                                      str(rid) if rid else None)}

    def run_mark(self, b: dict) -> dict:
        self.runs.mark(str(b.get("id", "")), bool(b.get("active")), float(b.get("ttl", 60)))
        return {"runs_active": self.runs.active()}

    def health(self) -> dict:
        with self._active_lock:
            active = self._active
        return {"ok": True, "version": VERSION, "runs_active": self.runs.active(),
                "requests_active": active,
                "idle_s": 0.0 if active else time.monotonic() - self._last}


_JSON_ROUTES = {
    "/fs/list": Agent.fs_list, "/fs/stat": Agent.fs_stat, "/fs/mkdir": Agent.fs_mkdir,
    "/fs/delete": Agent.fs_delete, "/fs/rename": Agent.fs_rename,
    "/git/status": Agent.git_status, "/git/diff": Agent.git_diff,
    "/git/remote_of": Agent.git_remote_of, "/git/commit": Agent.git_commit,
    "/git/recoverable": Agent.git_recoverable, "/git/pull": Agent.git_pull,
    "/git/push": Agent.git_push, "/git/hold": Agent.git_hold,
    "/git/set_remote": Agent.git_set_remote,
    "/git/release": Agent.git_release, "/runs": Agent.run_start,
    "/managed/delete": Agent.managed_delete, "/managed/move": Agent.managed_move,
    "/ignore/entries": Agent.ignore_entries, "/ignore/set": Agent.ignore_set,
    "/quarantine/view": Agent.quarantine_view, "/project/boards": Agent.project_boards,
    "/worktree/ensure": Agent.worktree_ensure, "/worktree/remove": Agent.worktree_remove,
    "/runs/mark": Agent.run_mark,
}


def make_handler(agent: Agent) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = REQUEST_TIMEOUT_S

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
            logger.info("%s", fmt % args)

        def _send(self, status: int, body: bytes, ctype: str = "application/json",
                  headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj).encode())

        def _body(self) -> bytes:
            return read_body(self, MAX_BODY, required=self.command in ("POST", "PUT"))

        def _dispatch(self, method: str) -> None:
            try:
                if not agent.authorised(self.headers.get("Authorization")):
                    # Not read at all: closing the connection disposes of it.
                    self.close_connection = True
                    raise AgentError(401, "not authorised", "unauthorised")
                url = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                path = url.path
                if method == "GET" and path == "/health":
                    return self._json(200, agent.health())
                agent.touch()
                if method == "POST" and path == "/fs/write":
                    return self._json(200, agent.fs_write(q, self._body()))
                if method == "POST" and path == "/quarantine/accept_upload":
                    return self._json(200, agent.quarantine_accept(q, self._body()))
                if method == "POST" and path == "/fs/read":
                    body = json.loads(self._body() or b"{}")
                    return self._send(200, agent.fs_read(body), "application/octet-stream")
                if method == "POST" and path in _JSON_ROUTES:
                    body = json.loads(self._body() or b"{}")
                    if not isinstance(body, dict):
                        raise AgentError(400, "body must be an object")
                    return self._json(200, _JSON_ROUTES[path](agent, body))
                if path.startswith("/runs/"):
                    rest = path[len("/runs/"):].split("/")
                    if method == "GET" and len(rest) == 1:
                        run = agent.runs.get(rest[0])
                        offset = int(q.get("offset", "0"))
                        with open(run.log, "rb") as fh:
                            fh.seek(offset)
                            data = fh.read(1024 * 1024)
                        done = run.exit_code is not None
                        return self._send(200, data, "application/octet-stream", {
                            "X-Run-Offset": str(offset + len(data)),
                            "X-Run-Done": "1" if done else "0",
                            "X-Run-Exit": "" if run.exit_code is None else str(run.exit_code),
                        })
                    if method == "POST" and len(rest) == 2 and rest[1] == "cancel":
                        self._body()
                        agent.runs.cancel(rest[0])
                        return self._json(200, {"ok": True})
                raise AgentError(404, f"no route {method} {path}", "notfound")
            except AgentError as e:
                self._json(e.status, {"error": str(e), "kind": e.kind})
            except _projects.ProjectError as e:
                self._json(400, {"error": str(e), "kind": "project"})
            except json.JSONDecodeError:
                self._json(400, {"error": "body is not JSON", "kind": "error"})
            except Exception as e:  # noqa: BLE001 - one request must not kill the agent
                logger.exception("request failed")
                self._json(500, {"error": f"{type(e).__name__}: {e}", "kind": "error"})

        def _counted(self, method: str) -> None:
            if urlsplit(self.path).path == "/health":
                return self._dispatch(method)
            agent.begin()
            try:
                self._dispatch(method)
            finally:
                agent.end()

        def do_GET(self) -> None:  # noqa: N802
            self._counted("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._counted("POST")

    return Handler


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _undumpable() -> None:
    """PR_SET_DUMPABLE 0: /proc/<pid>/{mem,environ} become unreadable to the
    same uid, so a run cannot lift the token out of this process."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
    except (OSError, AttributeError):  # pragma: no cover
        logger.warning("could not mark the agent non-dumpable")


def read_token(path: Path) -> str:
    """Read the token the control plane left for this start, and remove it."""
    token = path.read_text().strip()
    path.unlink()
    return token


def serve(socket_path: str, agent: Agent) -> Server:
    p = Path(socket_path)
    if p.exists() or p.is_symlink():
        p.unlink()
    server = Server(str(p), make_handler(agent))
    os.chmod(p, 0o600)
    return server


def main() -> None:
    if sys.argv[1:2] == ["--connect"] and len(sys.argv) == 4:
        sys.exit(connect_main(sys.argv[2], sys.argv[3]))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s")
    run_dir = Path(os.environ.get("BLPL_AGENT_DIR", "/run/blpl"))
    roots = [r for r in os.environ.get("BLPL_AGENT_ROOTS", "").split(":") if r]
    token_file = run_dir / "agent.token"
    if not run_dir.is_dir() or not token_file.is_file():
        # Phase 2 workspaces have no agent directory. They are kept alive and
        # driven by docker exec, so this just holds the container open.
        logger.info("no agent directory or token; not serving")
        signal.sigwait({signal.SIGTERM, signal.SIGINT})
        return
    _undumpable()
    harden_git_environment(os.environ)
    agent = Agent(read_token(token_file), roots,
                  allow_runs=os.environ.get("BLPL_AGENT_RUNS") == "1")
    server = serve(str(run_dir / "agent.sock"), agent)
    signal.signal(signal.SIGTERM, lambda *a: threading.Thread(target=server.shutdown).start())
    logger.info("serving on %s for %s", run_dir / "agent.sock", roots)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
