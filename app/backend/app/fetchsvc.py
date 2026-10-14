"""Documents fetched by the control plane for runs in workspaces.

A workspace has no general internet: Squid lets a run reach LLM, distributor
API and git hosts only (egress.py). A datasheet lives on whatever host its
manufacturer chose, so a run asks this service instead (blpl.core.fetcher),
which the worker serves on the workspaces' own network.

What the service does, in order:

* authenticates the request with the run's own egress grant, the credential
  the worker issued for that run alone;
* refuses anything but http(s) on ports 80 and 443, and any host that
  resolves to a private, loopback, link-local, multicast or reserved address
  (the control plane, the host, the cloud metadata service), on the first
  request and on every redirect, connecting to the address it checked rather
  than resolving twice;
* caps the size and the time, and requires the bytes to be a PDF;
* puts the bytes through quarantine's own decision (``quarantine.assess``),
  with this deployment's scanner and scan requirement.

Only bytes quarantine accepts go back. A held file is kept here as evidence
(``data/fetch-held/<project>/``) with its verdict, and the run is told why;
its bytes never reach the workspace. The run then files what it was given
through the project's own quarantine, so the ledger reads as for any fetch.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import socket
import ssl
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger("blpl.fetch")

MAX_BYTES = int(os.environ.get("BLPL_FETCH_MAX_MB", "50")) * 1024 * 1024
TIMEOUT_S = 30.0
# The whole fetch, redirects included, however the server paces its bytes. A
# per-read socket timeout alone let a server trickle one byte per timeout
# and keep a fetch going forever (Codex, PR #20).
DEADLINE_S = float(os.environ.get("BLPL_FETCH_DEADLINE_S", "120"))
# Fetches at once, across every run, and per run. Beyond either: 429.
MAX_CONCURRENT = int(os.environ.get("BLPL_FETCH_MAX_CONCURRENT", "8"))
MAX_PER_RUN = int(os.environ.get("BLPL_FETCH_MAX_PER_RUN", "2"))
# Held files kept as evidence, per project.
HELD_MAX_BYTES = int(os.environ.get("BLPL_FETCH_HELD_MAX_MB", "500")) * 1024 * 1024
MAX_REDIRECTS = 5
# A fetch request is a URL in a small JSON object.
MAX_REQUEST_BYTES = 64 * 1024
REQUEST_TIMEOUT_S = 30.0
_PORTS = {"http": 80, "https": 443}


class Refused(Exception):
    """A fetch this service will not make, said to the run as it is."""


Resolve = Callable[[str, int], list[str]]


def _resolve(host: str, port: int) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})


def _public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified)


def _target(url: str, resolve: Resolve) -> tuple[str, str, int, str, str]:
    """(scheme, host, port, path, address) for url, or Refused."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in _PORTS:
        raise Refused(f"only http and https can be fetched, not {scheme or 'no scheme'}")
    host = (parts.hostname or "").lower()
    if not host:
        raise Refused("the URL names no host")
    try:
        port = parts.port or _PORTS[scheme]
    except ValueError:
        raise Refused("the URL's port is not a number") from None
    if port not in (80, 443):
        raise Refused(f"only ports 80 and 443 can be fetched, not {port}")
    try:
        addrs = resolve(host, port)
    except OSError as exc:
        raise Refused(f"{host} could not be resolved: {exc}") from None
    if not addrs:
        raise Refused(f"{host} has no address")
    bad = [a for a in addrs if not _public(a)]
    if bad:
        raise Refused(f"{host} resolves to a private or local address ({bad[0]}); "
                      "the control plane does not fetch from those")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port, path, addrs[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to the address that was checked, with the certificate checked
    against the name in the URL."""

    def __init__(self, host: str, port: int, address: str, **kw):
        super().__init__(host, port, context=ssl.create_default_context(), **kw)
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, **kw):
        super().__init__(host, port, **kw)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


@dataclass
class Download:
    url: str
    data: bytes


def download(url: str, *, resolve: Resolve = _resolve, max_bytes: int = MAX_BYTES,
             timeout: float = TIMEOUT_S, connections=None,
             deadline_s: float | None = None) -> Download:
    """GET url, following redirects, each hop checked. Raises Refused.

    Streamed, with the size cap enforced as bytes arrive and one monotonic
    deadline over the whole fetch: each read is given only what is left of
    it."""
    import time

    connections = connections or {"https": _PinnedHTTPS, "http": _PinnedHTTP}
    limit = DEADLINE_S if deadline_s is None else deadline_s
    deadline = time.monotonic() + limit

    def left() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refused(f"the fetch took longer than {limit:g} s")
        return remaining

    for _ in range(MAX_REDIRECTS + 1):
        scheme, host, port, path, address = _target(url, resolve)
        conn = connections[scheme](host, port, address, timeout=min(timeout, left()))
        try:
            conn.request("GET", path, headers={
                "Host": host if port == _PORTS[scheme] else f"{host}:{port}",
                "User-Agent": "BLPL datasheet fetcher", "Accept": "application/pdf,*/*"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location") or ""
                if not location:
                    raise Refused(f"{url} redirected nowhere")
                url = urljoin(url, location)
                continue
            if resp.status != 200:
                raise Refused(f"{host} answered {resp.status}")
            length = resp.getheader("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise Refused(f"the file is larger than {max_bytes // (1024 * 1024)} MB")
            data = bytearray()
            while True:
                sock = getattr(conn, "sock", None)
                if sock is not None:
                    sock.settimeout(min(timeout, left()))
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > max_bytes:
                    raise Refused(f"the file is larger than {max_bytes // (1024 * 1024)} MB")
                left()
            return Download(url=url, data=bytes(data))
        except (OSError, http.client.HTTPException) as exc:
            raise Refused(f"{host} could not be fetched: {exc}") from None
        finally:
            conn.close()
    raise Refused(f"more than {MAX_REDIRECTS} redirects")


def assess(project: str, got: Download, held_root: Path) -> tuple[int, dict, bytes]:
    """(status, verdict, body) for what was downloaded."""
    from blpl.core import quarantine

    data = got.data
    sha = hashlib.sha256(data).hexdigest()
    if b"%PDF-" not in data[:1024]:
        return 422, {"held": True, "sha256": sha, "bytes": len(data),
                     "reasons": ["it is not a PDF; the control plane fetches documents only"],
                     "inspection": {}, "scan": {}}, b""
    a = quarantine.assess(data, is_pdf=True)
    if a.reasons:
        where = held_root / project
        where.mkdir(parents=True, exist_ok=True)
        kept = sum(f.stat().st_size for f in where.glob("*.pdf") if f.is_file())
        if kept + len(data) <= HELD_MAX_BYTES:
            (where / f"{sha}.pdf").write_bytes(data)
        else:
            # Evidence is bounded per project as well: past the cap the file
            # is still held and its verdict returned, but not kept here.
            logger.warning("held fetch for %s not kept as evidence: %s is over %d MB",
                           project, where, HELD_MAX_BYTES // (1024 * 1024))
        return 422, {"held": True, "sha256": sha, "bytes": len(data), "reasons": a.reasons,
                     "inspection": a.inspection.to_dict(), "scan": a.scan.to_dict()}, b""
    return 200, {"sha256": sha, "bytes": len(data)}, data


Authenticate = Callable[[str, str], "str | None"]


class Slots:
    """How many fetches may run at once, in all and per run. Taken without
    waiting: a fetch beyond either limit is refused, not queued, so no thread
    piles up behind a slow server."""

    def __init__(self, total: int = MAX_CONCURRENT, per_run: int = MAX_PER_RUN):
        self.total, self.per_run = total, per_run
        self._lock = threading.Lock()
        self._busy: dict[str, int] = {}

    def take(self, run: str) -> bool:
        with self._lock:
            if sum(self._busy.values()) >= self.total or self._busy.get(run, 0) >= self.per_run:
                return False
            self._busy[run] = self._busy.get(run, 0) + 1
            return True

    def give(self, run: str) -> None:
        with self._lock:
            n = self._busy.get(run, 0) - 1
            if n > 0:
                self._busy[run] = n
            else:
                self._busy.pop(run, None)


def make_handler(authenticate: Authenticate, held_root: Path, *,
                 resolve: Resolve = _resolve, connections=None, slots: Slots | None = None,
                 deadline_s: float | None = None):
    """The request handler. authenticate(user, password) returns the grant's
    project, or None."""
    slots = slots or Slots()

    class Handler(BaseHTTPRequestHandler):
        server_version = "blpl-fetch"
        # A request that trickles in holds one thread for this long at most.
        timeout = REQUEST_TIMEOUT_S

        _user = ""

        def _answer(self, status: int, payload: dict | None = None, body: bytes = b"",
                    ctype: str = "application/json") -> None:
            out = body if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype if payload is None else "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_POST(self) -> None:  # noqa: N802 - http.server's name
            if self.path != "/fetch":
                return self._answer(404, {"error": "not found"})
            project = self._project()
            if project is None:
                return self._answer(401, {"error": "this run's credential was not accepted"})
            from .sandbox.agent import AgentError, read_body

            try:
                # min(-1, cap) was -1, and read(-1) reads until the peer
                # closes (Codex, PR #20): the length is checked first.
                raw = read_body(self, MAX_REQUEST_BYTES, required=True)
            except AgentError as exc:
                return self._answer(exc.status, {"error": str(exc)})
            try:
                request = json.loads(raw or b"{}")
                url = str(request.get("url") or "")
            except (ValueError, TypeError, AttributeError):
                return self._answer(400, {"error": "a JSON body with a url is required"})
            run = self._user or project
            if not slots.take(run):
                return self._answer(429, {"error": "too many fetches at once; try again shortly"})
            # The slot covers the whole of the work, not only the download:
            # assessment parses the PDF, may wait on the scanner and writes
            # evidence, and releasing the slot before it let a run keep
            # starting assessments without bound (Codex, PR #20). So the bytes
            # one run holds at once are at most MAX_PER_RUN fetches of
            # MAX_BYTES each.
            try:
                try:
                    got = download(url, resolve=resolve, connections=connections,
                                   deadline_s=deadline_s)
                except Refused as exc:
                    logger.info("refused fetch for %s: %s (%s)", project, exc, url)
                    return self._answer(400, {"error": str(exc)})
                status, verdict, data = assess(project, got, held_root)
                logger.info("fetch for %s: %s %s (%d bytes)", project,
                            "held" if status != 200 else "accepted", got.url, verdict["bytes"])
                if status != 200:
                    return self._answer(status, verdict)
                return self._answer(200, None, data, "application/pdf")
            finally:
                slots.give(run)

        def _project(self) -> str | None:
            raw = self.headers.get("Authorization") or ""
            if not raw.lower().startswith("basic "):
                return None
            try:
                user, _, password = base64.b64decode(raw[6:]).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                return None
            self._user = user
            try:
                return authenticate(user, password)
            except Exception:  # noqa: BLE001 - fail closed
                logger.exception("could not check a fetch credential")
                return None

        def log_message(self, fmt: str, *args) -> None:
            logger.debug(fmt, *args)

    return Handler


def db_authenticate(user: str, password: str) -> str | None:
    """A run's grant, checked against the database: its project, or None."""
    from . import egress
    from .db import SessionFactory

    with SessionFactory() as session:
        if not egress.authenticate(session, user, password):
            return None
        g = egress.grant(session, user)
        return g.project if g is not None and g.kind == "run" else None


def serve(listen: str, held_root: Path, authenticate: Authenticate = db_authenticate,
          **kw) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    server = ThreadingHTTPServer((host or "0.0.0.0", int(port)),
                                 make_handler(authenticate, held_root, **kw))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="fetch-service").start()
    logger.info("fetch service on %s", listen)
    return server
