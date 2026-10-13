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


def test_automatic_creation_over_a_read_only_remote_is_refused(unlocked, http_remote):
    import app.main as main

    url, _ = http_remote
    r = unlocked.post("/api/projects/init", json=init_body("p", remote=url, push_policy="auto"))
    assert r.status_code == 400 and "not write" in r.json()["detail"]
    assert not (main.PROJECTS_ROOT / "p").exists()


def test_manual_creation_over_a_read_only_remote_goes_ahead_with_a_warning(unlocked, http_remote):
    url, repo = http_remote
    r = unlocked.post("/api/projects/init", json=init_body("p", remote=url))
    assert r.status_code == 200, r.text
    assert "pushes will fail" in r.json()["warning"]
    # Manual: nothing was pushed.
    assert subprocess.run(["git", "for-each-ref"], cwd=repo, capture_output=True, text=True).stdout == ""


def _protect(repo, branch="main"):
    """Accept pushes over http, but reject any update to ``branch`` in a
    pre-receive hook — branch protection as a server enforces it: on the
    update itself, which a dry run never sends."""
    subprocess.run(["git", "config", "http.receivepack", "true"], cwd=repo, check=True)
    hook = repo / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "while read old new ref; do\n"
        f'  if [ "$ref" = "refs/heads/{branch}" ]; then\n'
        f'    echo "GH006: Protected branch update failed for refs/heads/{branch}." >&2; exit 1\n'
        "  fi\n"
        "done\n"
    )
    hook.chmod(0o755)


def test_the_probe_targets_the_branch_but_cannot_see_its_protection(unlocked, http_remote):
    # Stated as a test so the limitation is not forgotten: the dry run passes
    # a protected branch, because protection acts on the update it never sends.
    url, repo = http_remote
    _protect(repo)
    body = unlocked.post("/api/projects/check-remote", json={"url": url}).json()
    assert body["status"] == "ok"


def test_automatic_creation_into_a_protected_branch_is_refused_and_rolled_back(
    unlocked, http_remote
):
    # Codex P1 on #19: the probe passed and the first real push was rejected.
    # Creation now makes that first push itself, and a rejection undoes it.
    import app.main as main

    url, repo = http_remote
    _protect(repo)
    r = unlocked.post("/api/projects/init", json=init_body("p", remote=url, push_policy="auto"))
    assert r.status_code == 400, r.text
    assert "refused the project's first push" in r.json()["detail"]
    assert "Protected branch" in r.json()["detail"]
    assert not (main.PROJECTS_ROOT / "p").exists()
    assert unlocked.get("/api/projects").json() == []
    assert subprocess.run(["git", "for-each-ref"], cwd=repo, capture_output=True, text=True).stdout == ""


def test_a_rejected_first_push_does_not_keep_a_new_token(unlocked, monkeypatch):
    # The same rollback, for the token: tokens are https-only, so the remote
    # here is an https URL whose probe passes and whose first push is refused.
    import app.main as main
    from app.projects import ProjectError, RemoteProbe

    monkeypatch.setattr(main.projects, "probe_remote", lambda url, env=None, branch="main": RemoteProbe("ok", True, ""))

    def push(name, *, env=None, checkout=None, url=None, after=None):
        assert env and env.get("GIT_HTTP_TOKEN") == "tok-SECRET-123", "pushed with the probed credential"
        raise ProjectError("git push failed (1): remote: GH006: Protected branch update failed")

    monkeypatch.setattr(main.projects, "push", push)
    r = unlocked.post(
        "/api/projects/init",
        json=init_body("p", remote="https://git.example.org/o/p.git", push_policy="auto",
                       token="tok-SECRET-123", token_username="u"),
    )
    assert r.status_code == 400 and "Protected branch" in r.json()["detail"]
    assert "tok-SECRET-123" not in r.text
    assert not (main.PROJECTS_ROOT / "p").exists()
    assert unlocked.get("/api/settings/git").json()["endpoints"] == []


def test_automatic_creation_pushes_exactly_the_first_commits(unlocked, http_remote):
    import app.main as main

    url, repo = http_remote
    subprocess.run(["git", "config", "http.receivepack", "true"], cwd=repo, check=True)
    r = unlocked.post("/api/projects/init", json=init_body("p", remote=url, push_policy="auto"))
    assert r.status_code == 200, r.text
    local = main.PROJECTS_ROOT / "p"
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=local, capture_output=True, text=True).stdout.strip()
    refs = subprocess.run(["git", "for-each-ref", "--format=%(refname) %(objectname)"], cwd=repo,
                          capture_output=True, text=True).stdout.split("\n")
    assert [r for r in refs if r] == [f"refs/heads/main {head}"]
    st = unlocked.get("/api/projects/p/git/status").json()
    assert st["last_push"]["ok"] is True and st["ahead"] == 0


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


# -- clones -------------------------------------------------------------------


def _seed(repo) -> None:
    """Give the served repository a commit to clone, written straight to disk
    rather than over http (whose receive-pack these tests switch off)."""
    import tempfile

    work = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", "-b", "main", work], check=True)
    subprocess.run(
        ["git", "-c", "user.email=s@example.com", "-c", "user.name=s", "commit", "-q",
         "--allow-empty", "-m", "seed"], cwd=work, check=True,
    )
    subprocess.run(["git", "push", "-q", str(repo), "main"], cwd=work, check=True)


def _clone_body(url, policy):
    from conftest import clone_body

    return clone_body("c", url, push_policy=policy)


def test_an_automatic_clone_over_a_read_only_remote_is_refused(unlocked, http_remote):
    # Codex P2 on #19: the clone read fine, the automatic policy was recorded,
    # and every later push failed.
    import app.main as main

    url, repo = http_remote
    _seed(repo)
    r = unlocked.post("/api/projects/clone", json=_clone_body(url, "auto"))
    assert r.status_code == 400 and "not write" in r.json()["detail"]
    assert not (main.PROJECTS_ROOT / "c").exists()
    assert unlocked.get("/api/projects").json() == []
    # Clone takes no new token; nothing was added to the store either.
    assert unlocked.get("/api/settings/git").json()["endpoints"] == []


def test_a_manual_clone_over_a_read_only_remote_is_allowed_with_a_warning(unlocked, http_remote):
    import app.main as main

    url, repo = http_remote
    _seed(repo)
    r = unlocked.post("/api/projects/clone", json=_clone_body(url, "manual"))
    assert r.status_code == 200, r.text
    assert "pushes will fail" in r.json()["warning"]
    assert (main.PROJECTS_ROOT / "c" / ".git").is_dir()


def test_an_automatic_clone_over_a_writable_remote_is_accepted(unlocked, http_remote):
    url, repo = http_remote
    _seed(repo)
    subprocess.run(["git", "config", "http.receivepack", "true"], cwd=repo, check=True)
    r = unlocked.post("/api/projects/clone", json=_clone_body(url, "auto"))
    assert r.status_code == 200, r.text
    assert r.json()["warning"] == ""
    assert unlocked.get("/api/projects/c/git/status").json()["push_policy"] == "auto"
    # The write check wrote nothing.
    refs = subprocess.run(["git", "for-each-ref", "--format=%(refname)"], cwd=repo,
                          capture_output=True, text=True).stdout.split()
    assert refs == ["refs/heads/main"]


def test_a_clone_into_a_protected_branch_records_the_first_rejected_push(
    unlocked, http_remote, monkeypatch
):
    # A clone cannot push for real to find out without changing the branch,
    # so the dry run (which passes protection) is all it gets; the first real
    # push is then rejected, and that is recorded as a failed push.
    import app.main as main

    url, repo = http_remote
    _seed(repo)
    _protect(repo)
    held = []

    class Deferred:
        def submit(self, fn):
            held.append(fn)

    monkeypatch.setattr(main, "AUTOPUSH", Deferred())
    r = unlocked.post("/api/projects/clone", json=_clone_body(url, "auto"))
    assert r.status_code == 200 and r.json()["warning"] == ""

    unlocked.put("/api/projects/c/files/design.md", json={"content": "# mine\n"})
    assert len(held) == 1
    held[0]()
    st = unlocked.get("/api/projects/c/git/status").json()
    assert st["last_push"]["ok"] is False
    assert "Protected branch" in st["last_push"]["error"]


def test_an_automatic_clone_refuses_a_remote_that_does_not_answer(unlocked, monkeypatch, tmp_path):
    import app.main as main
    import app.projects as projects_mod

    real = subprocess.run
    remote = bare_remote()
    _seed(remote)

    def run(argv, *a, **kw):
        if argv[:2] == ["git", "push"]:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout", 60))
        return real(argv, *a, **kw)

    monkeypatch.setattr(projects_mod.subprocess, "run", run)
    r = unlocked.post("/api/projects/clone", json=_clone_body(remote, "auto"))
    assert r.status_code == 400 and "did not answer" in r.json()["detail"]
    assert not (main.PROJECTS_ROOT / "c").exists()


@pytest.mark.parametrize("branch", ["main", "dev"])
def test_the_write_check_aims_at_the_branch_that_will_be_pushed(tmp_path, monkeypatch, branch):
    # Codex P1 on #19: it aimed at a side ref nobody protects, while the real
    # push updates refs/heads/<branch>, where providers scope permissions.
    import app.projects as projects_mod

    seen = []
    real = subprocess.run

    def run(argv, *a, **kw):
        if argv[:2] == ["git", "push"]:
            seen.append(argv)
        return real(argv, *a, **kw)

    monkeypatch.setattr(projects_mod.subprocess, "run", run)
    remote = bare_remote()
    assert projects_mod.Projects(tmp_path).probe_write(remote, branch=branch) is None
    (argv,) = seen
    assert "--dry-run" in argv
    assert argv[-1].endswith(f":refs/heads/{branch}")
    # And still writes nothing.
    assert real(["git", "for-each-ref"], cwd=remote, capture_output=True, text=True).stdout == ""


# -- reading and writing are separate facts; validation before side effects --


def test_a_non_empty_remote_is_refused_even_when_the_credential_cannot_write(unlocked, http_remote):
    # Codex P1 on #19: the write refusal replaced the read result, the
    # emptiness check ran only for "ok", and a manual project was created over
    # existing history.
    import app.main as main

    url, repo = http_remote
    _seed(repo)
    r = unlocked.post("/api/projects/init", json=init_body("p", remote=url))
    assert r.status_code == 409 and "already has commits" in r.json()["detail"]
    assert not (main.PROJECTS_ROOT / "p").exists()

    body = unlocked.post("/api/projects/check-remote", json={"url": url}).json()
    assert body["empty"] is False and body["writable"] is False
    assert "already has commits" in body["message"]
