"""The shipped Squid config and helper, in the real Squid image.

A grant is a per-run proxy credential, and the control plane decides each
host for it (app/egress.py). Here the decision is a table in this process,
served on the same Unix socket the backend serves, and Squid is the image
compose runs with app/egress/squid.conf and blpl-egress-helper.pl mounted as
in compose. A CONNECT that Squid allows goes on to fail at DNS (the names are
under .example), which is a 503; a refusal is a 403, and no credential is a
407.
"""

from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

IMAGE = "ubuntu/squid:6.10-24.10_beta"
EGRESS = Path(__file__).resolve().parents[2] / "egress"


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ok(), reason=f"docker or {IMAGE} not available")

GRANTS = {
    "g-alice": {"password": "alice-secret", "hosts": {"llm.alice.example"}},
    "g-bob": {"password": "bob-secret", "hosts": set()},
    "g-carol": {"password": "carol-secret", "hosts": {"llm.alice.example"}},
}
DEFAULTS = {"api.anthropic.com"}


class Decisions:
    def __init__(self):
        self.down = False
        self.acl_broken = False
        self.asked: list[dict] = []

    def __call__(self, request):
        self.asked.append(dict(request))
        if self.down:
            raise RuntimeError("control plane unavailable")
        g = GRANTS.get(request.get("user"))
        if g is None:
            return False
        if request["op"] == "auth":
            return request.get("password") == g["password"]
        if self.acl_broken:
            raise RuntimeError("database gone")
        return int(request["port"]) == 443 and (
            request["host"] in DEFAULTS or request["host"] in g["hosts"])


@pytest.fixture(scope="module")
def squid():
    from app import egress_server

    tmp = Path(tempfile.mkdtemp(prefix="blpl-squid-"))
    sockdir = tmp / "egress"
    sockdir.mkdir()
    os.chmod(tmp, 0o755)
    os.chmod(sockdir, 0o755)
    decisions = Decisions()
    server = egress_server.serve(str(sockdir / "decide.sock"), decisions)
    name = f"blpl-test-squid-{uuid.uuid4().hex[:8]}"
    subprocess.run([
        "docker", "run", "-d", "--rm", "--name", name, "--label", "blpl.test=squid",
        "-p", "127.0.0.1::3128",
        "-v", f"{EGRESS / 'squid.conf'}:/etc/squid/squid.conf:ro",
        "-v", f"{EGRESS}:/etc/squid/blpl:ro",
        "-v", f"{sockdir}:/run/blpl-egress:ro",
        IMAGE, "-f", "/etc/squid/squid.conf", "-NYC",
    ], check=True, capture_output=True)
    try:
        port = int(subprocess.run(["docker", "port", name, "3128/tcp"], capture_output=True,
                                  text=True, check=True).stdout.strip().rsplit(":", 1)[1])
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if _connect(port, "api.anthropic.com", 443, None)[0] == 407:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            pytest.fail(f"squid did not start:\n{logs.stdout}\n{logs.stderr}")
        yield port, decisions
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        server.shutdown()
        server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def _connect(port: int, host: str, target_port: int, cred: tuple[str, str] | None):
    s = socket.create_connection(("127.0.0.1", port), timeout=30)
    try:
        lines = [f"CONNECT {host}:{target_port} HTTP/1.1", f"Host: {host}:{target_port}"]
        if cred:
            token = base64.b64encode(f"{cred[0]}:{cred[1]}".encode()).decode()
            lines.append(f"Proxy-Authorization: Basic {token}")
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = s.recv(4096).decode("latin-1")
        return int(head.split(" ", 2)[1]), head
    finally:
        s.close()


def _allowed(status: int) -> bool:
    # Past the ACLs: Squid tried to reach the host and could not resolve it.
    return status not in (403, 407)


def test_no_credential_is_refused(squid):
    port, _ = squid
    assert _connect(port, "api.anthropic.com", 443, None)[0] == 407
    assert _connect(port, "api.anthropic.com", 443, ("g-alice", "wrong"))[0] == 407


def test_a_members_own_endpoint_is_reachable_from_their_run_only(squid):
    port, _ = squid
    assert _allowed(_connect(port, "llm.alice.example", 443, ("g-alice", "alice-secret"))[0])
    assert _connect(port, "llm.alice.example", 443, ("g-bob", "bob-secret"))[0] == 403
    # The defaults reach everyone; nothing else does.
    assert _allowed(_connect(port, "api.anthropic.com", 443, ("g-bob", "bob-secret"))[0])
    assert _connect(port, "elsewhere.example", 443, ("g-alice", "alice-secret"))[0] == 403
    # Only CONNECT, and only to 443 for these.
    assert _connect(port, "llm.alice.example", 8443, ("g-alice", "alice-secret"))[0] == 403


def test_a_failing_control_plane_is_a_refusal(squid):
    port, decisions = squid
    decisions.acl_broken = True
    try:
        # A host no one has asked about, so no cached answer stands in.
        GRANTS["g-carol"]["hosts"].add("fresh.alice.example")
        assert _connect(port, "fresh.alice.example", 443, ("g-carol", "carol-secret"))[0] == 403
    finally:
        decisions.acl_broken = False
    decisions.down = True
    try:
        GRANTS["g-dave"] = {"password": "dave-secret", "hosts": {"llm.alice.example"}}
        assert _connect(port, "llm.alice.example", 443, ("g-dave", "dave-secret"))[0] == 407
    finally:
        decisions.down = False
