"""Test connection, and creation, check what a push will need.

ls-remote reads through upload-pack, so it said yes to a token that could see
an empty private repository and not write to it; the token was saved, the
project created, and the first push failed (Codex, PR #19). The probe now
also asks receive-pack, with a dry-run push. And a remote that does not
answer is a result, not a 500.

The read-only remote here is real smart HTTP: git's own http-backend, which
serves upload-pack to anyone and refuses receive-pack unless it is enabled
for the repository — the same refusal, at the same step, that a provider
gives a read-only token.
"""

from __future__ import annotations

import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import bare_remote, init_body


def _serve(root: str):
    """A smart-HTTP git server over ``root`` (git http-backend as CGI)."""

    class Handler(BaseHTTPRequestHandler):
        def _cgi(self):
            path, _, query = self.path.partition("?")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            env = {
                **os.environ,
                "GIT_PROJECT_ROOT": root, "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": path, "QUERY_STRING": query,
                "REQUEST_METHOD": self.command, "REMOTE_ADDR": "127.0.0.1",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(len(body)),
            }
            if self.headers.get("Content-Encoding"):
                env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
            out = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True).stdout
            head, _, payload = out.partition(b"\r\n\r\n")
            status, headers = 200, []
            for line in head.decode().split("\r\n"):
                k, _, v = line.partition(":")
                if k.lower() == "status":
                    status = int(v.strip().split()[0])
                elif k:
                    headers.append((k, v.strip()))
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _cgi

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def http_remote(tmp_path, monkeypatch):
    """(url, repo path) for an empty bare repository served over http.
    receive-pack is off until a test turns it on."""
    monkeypatch.setenv("BLPL_GIT_ALLOWED_PROTOCOLS", "https:ssh:file:http")
    root = tmp_path / "srv"
    root.mkdir()
    repo = root / "board.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(repo)], check=True)
    server = _serve(str(root))
    yield f"http://127.0.0.1:{server.server_port}/board.git", repo
    server.shutdown()


def test_a_remote_that_can_be_read_but_not_written_is_not_ready(unlocked, http_remote):
    url, _ = http_remote
    # Readable: upload-pack answers, the repository is empty.
    assert subprocess.run(["git", "ls-remote", url], capture_output=True).returncode == 0

    body = unlocked.post("/api/projects/check-remote", json={"url": url}).json()
    assert body["status"] == "read_only" and body["ok"] is False
    assert "not write" in body["message"]


def test_creation_over_a_read_only_remote_is_refused(unlocked, http_remote):
    import app.main as main

    url, _ = http_remote
    r = unlocked.post("/api/projects/init", json=init_body("p", remote=url))
    assert r.status_code == 400 and "not write" in r.json()["detail"]
    assert not (main.PROJECTS_ROOT / "p").exists()


def test_a_writable_remote_passes_and_the_check_writes_nothing(unlocked, http_remote):
    url, repo = http_remote
    subprocess.run(["git", "config", "http.receivepack", "true"], cwd=repo, check=True)
    body = unlocked.post("/api/projects/check-remote", json={"url": url}).json()
    assert body["status"] == "ok" and body["empty"] is True
    # A dry run: no ref, no object, nothing anyone would see.
    assert subprocess.run(["git", "for-each-ref"], cwd=repo, capture_output=True, text=True).stdout == ""
    objects = subprocess.run(["git", "count-objects", "-v"], cwd=repo, capture_output=True, text=True).stdout
    assert "count: 0" in objects and "in-pack: 0" in objects


def test_the_write_check_leaves_a_local_bare_remote_untouched(unlocked):
    remote = bare_remote()
    assert unlocked.post("/api/projects/check-remote", json={"url": remote}).json()["status"] == "ok"
    assert subprocess.run(["git", "for-each-ref"], cwd=remote, capture_output=True, text=True).stdout == ""


@pytest.mark.parametrize("stalls", ["ls-remote", "push"])
def test_a_remote_that_does_not_answer_is_a_result_not_a_500(unlocked, monkeypatch, stalls):
    import app.projects as projects_mod

    real = subprocess.run

    def run(argv, *a, **kw):
        if argv[:2] == ["git", stalls]:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout", 60))
        return real(argv, *a, **kw)

    monkeypatch.setattr(projects_mod.subprocess, "run", run)
    remote = bare_remote()
    r = unlocked.post("/api/projects/check-remote", json={"url": remote})
    assert r.status_code == 200
    assert r.json()["status"] == "error" and r.json()["ok"] is False
    r = unlocked.post("/api/projects/init", json=init_body("p", remote=remote))
    assert r.status_code == 400 and "did not answer" in r.json()["detail"]
