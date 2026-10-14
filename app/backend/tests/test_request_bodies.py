"""Every server this PR adds reads a request body with a bound on it.

``Content-Length: -1`` once reached ``rfile.read(-1)``, which reads until the
peer closes: one request could buffer without limit and hold a thread forever
(Codex, PR #20). For each server: a negative length, a non-integer one, one
over the cap, and a body that trickles and never finishes are all refused
promptly, without reading what the request did not promise.
"""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from app.sandbox import agent as agent_mod

TOKEN = "t" * 64


def _exchange(connect, request: bytes, *, then_wait: float = 0.0, limit: float = 8.0) -> bytes:
    """Send request, keep the connection open, and return whatever comes back
    before the server closes it or `limit` passes."""
    s = connect()
    s.settimeout(limit)
    try:
        s.sendall(request)
    except (BrokenPipeError, ConnectionResetError):
        # The server refused and closed before the client finished sending:
        # that is the refusal working. What it said, if anything, is read
        # below; a closed connection reads as no response.
        pass
    if then_wait:
        time.sleep(then_wait)
    got = b""
    start = time.monotonic()
    try:
        while time.monotonic() - start < limit:
            chunk = s.recv(65536)
            if not chunk:
                break
            got += chunk
            if b"\r\n\r\n" in got and b"Content-Length" in got:
                head, _, body = got.partition(b"\r\n\r\n")
                n = int(head.split(b"Content-Length:")[1].split(b"\r\n")[0])
                if len(body) >= n:
                    break
    except (TimeoutError, OSError):
        pass  # reset after a refusal included: whatever arrived first counts
    finally:
        s.close()
    return got


def _status(raw: bytes) -> int | None:
    try:
        return int(raw.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return None


def _unix(path: str):
    def connect():
        s = socket.socket(socket.AF_UNIX)
        s.connect(path)
        return s
    return connect


def _tcp(port: int):
    return lambda: socket.create_connection(("127.0.0.1", port))


def _post(path: str, length: str | None, extra: str = "", auth: bool = True) -> bytes:
    lines = [f"POST {path} HTTP/1.1", "Host: x", "Content-Type: application/json"]
    if auth:
        lines.append(f"Authorization: Bearer {TOKEN}")
    if length is not None:
        lines.append(f"Content-Length: {length}")
    if extra:
        lines.append(extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


# -- the workspace agent -------------------------------------------------------------------


@pytest.fixture
def agent_sock(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "REQUEST_TIMEOUT_S", 1.0)
    (tmp_path / "board").mkdir()
    a = agent_mod.Agent(TOKEN, [str(tmp_path / "board")])
    path = str(tmp_path / "agent.sock")
    server = agent_mod.serve(path, a)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield path, tmp_path / "board"
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("length, status", [
    ("-1", 400), ("abc", 400), ("1e3", 400), (" 12x", 400),
    (str(agent_mod.MAX_BODY + 1), 413), (None, 411),
])
def test_the_agent_refuses_a_body_it_was_not_promised_properly(agent_sock, length, status):
    path, board = agent_sock
    t0 = time.monotonic()
    raw = _exchange(_unix(path), _post(f"/fs/write?checkout={board}&path=a.md", length))
    assert _status(raw) == status, raw[:200]
    assert time.monotonic() - t0 < 5
    assert not (board / "a.md").exists()


def test_the_agent_refuses_a_chunked_body(agent_sock):
    path, board = agent_sock
    raw = _exchange(_unix(path), _post(f"/fs/write?checkout={board}&path=a.md", None,
                                       "Transfer-Encoding: chunked") + b"5\r\nhello\r\n")
    assert _status(raw) == 411


def test_the_agent_gives_up_on_a_body_that_never_finishes(agent_sock):
    path, board = agent_sock
    t0 = time.monotonic()
    raw = _exchange(_unix(path), _post(f"/fs/write?checkout={board}&path=a.md", "1000")
                    + b"only a little")
    assert time.monotonic() - t0 < 5, "the connection was held open"
    assert _status(raw) in (408, None)
    assert not (board / "a.md").exists()


def test_an_unauthorised_request_is_not_read(agent_sock):
    path, board = agent_sock
    t0 = time.monotonic()
    raw = _exchange(_unix(path), _post("/fs/write", "-1", auth=False))
    assert _status(raw) == 401 and time.monotonic() - t0 < 5


# -- the fetch service ---------------------------------------------------------------------


@pytest.fixture
def fetch_port(tmp_path, monkeypatch):
    from app import fetchsvc

    monkeypatch.setattr(fetchsvc, "REQUEST_TIMEOUT_S", 1.0)
    server = fetchsvc.serve("127.0.0.1:0", tmp_path / "held",
                            authenticate=lambda u, p: "board")
    yield server.server_port
    server.shutdown()
    server.server_close()


def _fetch_post(length: str | None, extra: str = "") -> bytes:
    import base64

    auth = base64.b64encode(b"g:t").decode()
    lines = ["POST /fetch HTTP/1.1", "Host: x", f"Authorization: Basic {auth}"]
    if length is not None:
        lines.append(f"Content-Length: {length}")
    if extra:
        lines.append(extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


@pytest.mark.parametrize("length, status", [
    ("-1", 400), ("abc", 400), (str(64 * 1024 + 1), 413), (None, 411)])
def test_the_fetch_service_refuses_a_bad_length(fetch_port, length, status):
    t0 = time.monotonic()
    raw = _exchange(_tcp(fetch_port), _fetch_post(length))
    assert _status(raw) == status, raw[:200]
    assert time.monotonic() - t0 < 5


def test_the_fetch_service_refuses_chunked_and_slow_bodies(fetch_port):
    assert _status(_exchange(_tcp(fetch_port),
                             _fetch_post(None, "Transfer-Encoding: chunked"))) == 411
    t0 = time.monotonic()
    raw = _exchange(_tcp(fetch_port), _fetch_post("500") + b'{"url":')
    assert time.monotonic() - t0 < 5
    assert _status(raw) in (408, None)


# -- the socket proxy ----------------------------------------------------------------------


class _NoUpstream:
    async def get_json(self, path):
        return 404, None

    async def open(self):  # pragma: no cover - nothing here reaches it
        raise AssertionError("forwarded")


def _proxy_exchange(request: bytes, monkeypatch, *, limit: float = 8.0) -> tuple[int | None, float]:
    from app.sandbox import proxy as proxy_mod
    from app.sandbox.policy import ProxyPolicy

    monkeypatch.setattr(proxy_mod, "REQUEST_TIMEOUT_S", 1.0)
    path = str(Path(tempfile.mkdtemp(prefix="blpl-px-")) / "p.sock")

    async def go():
        server = await proxy_mod.serve(proxy_mod.DockerProxy(ProxyPolicy(), _NoUpstream()), path)
        try:
            t0 = time.monotonic()
            raw = await asyncio.to_thread(_exchange, _unix(path), request, limit=limit)
            return _status(raw), time.monotonic() - t0
        finally:
            server.close()

    return asyncio.run(go())


@pytest.mark.parametrize("length, status", [("-1", 400), ("abc", 400), (str(2 * 1024 * 1024), 413)])
def test_the_socket_proxy_refuses_a_bad_length(monkeypatch, length, status):
    req = ("POST /v1.44/containers/create HTTP/1.1\r\nHost: d\r\n"
           f"Content-Length: {length}\r\n\r\n").encode()
    got, took = _proxy_exchange(req, monkeypatch)
    assert got == status and took < 5


def test_the_socket_proxy_caps_a_chunked_body_and_times_out_a_slow_one(monkeypatch):
    chunk = b"%x\r\n" % 65536 + b"x" * 65536 + b"\r\n"
    endless = (b"POST /v1.44/containers/create HTTP/1.1\r\nHost: d\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n" + chunk * 20)
    got, took = _proxy_exchange(endless, monkeypatch)
    # 413, or the connection closed on the client mid-send (the 413 lost to
    # the reset): refused either way, and promptly. Never a forward.
    assert got in (413, None) and took < 5
    slow = (b"POST /v1.44/containers/create HTTP/1.1\r\nHost: d\r\n"
            b"Content-Length: 100\r\n\r\n{")
    got, took = _proxy_exchange(slow, monkeypatch)
    assert got == 408 and took < 5


# -- the egress decision socket and the storage helper ------------------------------------


def test_the_egress_socket_drops_a_line_that_never_ends(tmp_path, monkeypatch):
    from app import egress_server

    monkeypatch.setattr(egress_server._Handler, "timeout", 1.0)
    path = str(tmp_path / "d.sock")
    server = egress_server.serve(path, lambda q: True)
    try:
        t0 = time.monotonic()
        got = _exchange(_unix(path), b'{"op": "auth", "user": "')
        assert got in (b"", b"ERR\n") and time.monotonic() - t0 < 5
        got = _exchange(_unix(path), b"x" * 100_000 + b"\n")
        # ERR, or closed while the rest was still arriving: never parsed.
        assert got.startswith(b"ERR\n") or got == b""
    finally:
        server.shutdown()
        server.server_close()


def test_the_storage_helper_drops_a_line_that_never_ends_or_is_too_long(tmp_path, monkeypatch):
    from app.sandbox import btrfs

    monkeypatch.setattr(btrfs, "HELPER_TIMEOUT_S", 1.0)

    class Never:
        def handle(self, request):  # pragma: no cover - never reached
            raise AssertionError("parsed")

    path = str(tmp_path / "s.sock")
    server = btrfs.serve_helper(path, Never(), client_uid=os.getuid(), client_gid=os.getgid())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        t0 = time.monotonic()
        got = _exchange(_unix(path), b'{"verb": "usage", "project": "')
        assert time.monotonic() - t0 < 5
        assert b'"ok": false' in got or got == b""
        got = _exchange(_unix(path), b"{" + b"x" * 100_000 + b"}\n")
        assert b'"ok": false' in got or got == b""
    finally:
        server.shutdown()
        server.server_close()
