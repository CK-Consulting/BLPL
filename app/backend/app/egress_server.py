"""The control plane's answer to Squid: may this grant reach this host?

Squid runs a small helper (app/egress/blpl-egress-helper.pl) for its basic
authentication and for an external ACL. The helper turns each of Squid's
questions into one JSON line on this Unix socket and Squid's answer into
OK or ERR. The socket lives in a directory only the backend and Squid mount
(app/data/egress), and it carries no secret: a question is a grant's own
credential or name, and the answer is a single yes or no.

Fail closed, at both ends. Here, any error, malformed line or timeout is a
no. In the helper, a socket it cannot reach, or an answer it cannot read, is
ERR. Squid caches answers briefly (external_acl_type ttl), so a change to the
administrators' list takes effect within that time and needs no reload.
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
import threading
from typing import Callable, Mapping

from .serving import Bounded, Deadline

logger = logging.getLogger("blpl.egress")

Decide = Callable[[Mapping], bool]
_MAX_LINE = 4096


class _Handler(socketserver.StreamRequestHandler):
    timeout = 5

    def handle(self) -> None:
        decide: Decide = self.server.decide  # type: ignore[attr-defined]
        while True:
            # The timeout above is per recv, so a peer sending one byte every
            # few seconds held a thread for as long as it liked. Each question
            # must arrive whole within it (Codex, PR #20).
            deadline = Deadline(self.connection, self.timeout)
            try:
                line = self.rfile.readline(_MAX_LINE + 1)
            except OSError:
                return
            finally:
                deadline.cancel()
            if deadline.expired.is_set() or not line or \
                    not line.endswith(b"\n") and len(line) <= _MAX_LINE:
                # Closed, or cut part way through a question: no answer.
                return
            ok = False
            try:
                if len(line) <= _MAX_LINE:
                    request = json.loads(line)
                    ok = isinstance(request, dict) and bool(decide(request))
            except Exception:  # noqa: BLE001 - every failure is a no
                logger.exception("egress decision failed")
                ok = False
            try:
                self.wfile.write(b"OK\n" if ok else b"ERR\n")
                self.wfile.flush()
            except OSError:
                return


class _Server(Bounded, socketserver.UnixStreamServer):
    # Squid runs at most ten helpers (squid.conf: five for auth, five for the
    # ACL), each with one connection; more than this is not Squid.
    daemon_threads = True
    allow_reuse_address = True
    max_connections = 32


def serve(path: str, decide: Decide) -> _Server:
    """Listen on path, answering with decide(request). Returns the server,
    already serving on a daemon thread; shutdown() and server_close() stop it."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    server = _Server(path, _Handler)
    server.decide = decide  # type: ignore[attr-defined]
    # Squid's helpers run as Squid's own user, which shares no uid or group
    # with this process; the directory, mounted into the backend and Squid
    # only, is what limits who can ask.
    os.chmod(path, 0o666)
    threading.Thread(target=server.serve_forever, daemon=True, name="egress-decide").start()
    return server


def db_decide(request: Mapping) -> bool:
    """decide() for the deployment: each question in its own session."""
    from . import egress
    from .db import SessionFactory

    with SessionFactory() as session:
        return egress.decide(session, request)
