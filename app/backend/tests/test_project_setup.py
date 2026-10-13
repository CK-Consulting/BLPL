"""Creating a project asks for what it needs not to lose work on day one.

A name used to be enough, and the result was a project whose only copy was
this server. Creation now takes an empty remote it can reach with the
creator's credential, a push policy chosen on purpose, and the creator's
commit identity — and with automatic pushing, every commit goes out behind
the save that made it, with a failure recorded rather than lost.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import bare_remote, clone_body, init_body

TOKEN = "glpat-SECRET-token-0123456789abcdef"


def _git(d, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=d, capture_output=True, text=True, check=True
    ).stdout.strip()


class Deferred:
    """An executor that holds jobs until the test runs them.

    Not inline: a job run inside the request would write its record while the
    request's own transaction is still open, which SQLite serialises into a
    lock wait. Production runs it on a thread after the fact; so does this,
    only at a moment the test chooses.
    """

    def __init__(self):
        self.jobs = []

    def submit(self, fn):
        self.jobs.append(fn)

    def run(self) -> int:
        jobs, self.jobs = self.jobs, []
        for job in jobs:
            job()
        return len(jobs)


@pytest.fixture
def pushes(client, monkeypatch):
    import app.main as main

    d = Deferred()
    monkeypatch.setattr(main, "AUTOPUSH", d)
    return d


def _remote_with_history(tmp_path: Path) -> str:
    work = tmp_path / "seed"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "README.md").write_text("hello\n")
    _git(work, "add", "-A")
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "x"],
                   cwd=work, check=True)
    remote = bare_remote()
    _git(work, "push", "-q", remote, "main")
    return remote


# -- validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    "override,status,why",
    [
        ({"remote": ""}, 400, "a remote repository URL is required"),
        ({"remote": "https://user:hunter2-token@github.com/o/p.git"}, 400, "carries a password or token"),
        ({"remote": "https://github.com/o/p.git?access_token=hunter2-token"}, 400, "query string"),
        ({"push_policy": None}, 400, "choose a push policy"),
        ({"push_policy": "sometimes"}, 400, "choose a push policy"),
        ({"git_email": "not-an-address"}, 400, "does not look like an address"),
        ({"git_name": ""}, 400, "commit name is required"),
        ({"git_name": "Two\nLines"}, 400, "control characters"),
    ],
)
def test_creation_refuses_what_it_cannot_use(unlocked, override, status, why):
    import app.main as main

    r = unlocked.post("/api/projects/init", json=init_body("p", **override))
    assert r.status_code == status, r.text
    assert why in r.json()["detail"]
    assert "hunter2" not in r.text
    # Refused before the disk was touched.
    assert not (main.PROJECTS_ROOT / "p").exists()
    assert unlocked.get("/api/projects").json() == []


def test_a_remote_that_already_has_history_is_refused_and_points_at_clone(unlocked, tmp_path):
    import app.main as main

    remote = _remote_with_history(tmp_path)
    head = _git(remote, "rev-parse", "main")
    r = unlocked.post("/api/projects/init", json=init_body("p", remote=remote))
    assert r.status_code == 409
    assert "already has commits" in r.json()["detail"] and "Clone" in r.json()["detail"]
    assert not (main.PROJECTS_ROOT / "p").exists()
    # Neither force-pushed over nor merged into.
    assert _git(remote, "rev-parse", "main") == head


def test_an_unreachable_remote_is_refused(unlocked, tmp_path):
    r = unlocked.post("/api/projects/init", json=init_body("p", remote=str(tmp_path / "nothing.git")))
    assert r.status_code == 400
    assert "No repository was found" in r.json()["detail"]


def test_the_creators_identity_can_also_become_the_default(unlocked):
    r = unlocked.post(
        "/api/projects/init",
        json=init_body("p", git_name="New Default", git_email="nd@example.com", save_as_default=True),
    )
    assert r.status_code == 200, r.text
    assert unlocked.get("/api/me/git-identity").json()["identity"] == {
        "name": "New Default", "email": "nd@example.com",
    }


def test_the_dialog_is_prefilled_from_the_account_default(unlocked):
    got = unlocked.get("/api/projects/new/defaults").json()
    assert got["identity"] == {"name": "Test user_test", "email": "test@example.com"}
    assert got["identity_is_default"] is True
    assert "not zero" in got["manual_push_warning"]


# -- test connection ----------------------------------------------------------


def test_check_remote_reports_an_empty_repository(unlocked):
    r = unlocked.post("/api/projects/check-remote", json={"url": bare_remote()})
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["empty"] is True


def test_check_remote_says_when_a_repository_is_not_empty(unlocked, tmp_path):
    r = unlocked.post("/api/projects/check-remote", json={"url": _remote_with_history(tmp_path)})
    body = r.json()
    assert body["status"] == "ok" and body["empty"] is False
    assert "already has commits" in body["message"]


def test_check_remote_reports_not_found(unlocked, tmp_path):
    body = unlocked.post("/api/projects/check-remote", json={"url": str(tmp_path / "x.git")}).json()
    assert body["status"] == "not_found" and body["ok"] is False


def test_a_token_is_never_echoed_by_test_connection(unlocked):
    # Nothing listens on port 9, so git fails fast after being handed the
    # token through the credential helper; the answer must not carry it.
    r = unlocked.post(
        "/api/projects/check-remote",
        json={"url": "https://127.0.0.1:9/o/p.git", "token": TOKEN, "token_username": "oauth2"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert TOKEN not in r.text
    # Nor was it saved: testing a token is not keeping it.
    assert unlocked.get("/api/settings/git").json()["endpoints"] == []


def test_a_token_is_only_for_an_https_remote(unlocked):
    r = unlocked.post(
        "/api/projects/check-remote", json={"url": bare_remote(), "token": TOKEN}
    )
    assert r.status_code == 400 and "https://" in r.json()["detail"]
    assert TOKEN not in r.text


def test_a_new_token_goes_into_the_credential_store_and_nowhere_else(unlocked, monkeypatch):
    import app.main as main
    from app.projects import RemoteProbe

    url = "https://git.example.org/o/board.git"
    seen_env = {}

    def probe(remote, *, env=None):
        # The token reached git the way every credential does — leased into
        # the environment for the one command — not through the URL.
        seen_env.update(env or {})
        return RemoteProbe("ok", empty=True, message="")

    monkeypatch.setattr(main.projects, "probe_remote", probe)
    r = unlocked.post(
        "/api/projects/init",
        json=init_body("p", remote=url, token=TOKEN, token_username="oauth2"),
    )
    assert r.status_code == 200, r.text
    assert seen_env.get("GIT_HTTP_TOKEN") == TOKEN
    assert TOKEN not in r.text

    # Saved, sealed, into the one per-user store, matched to the remote's host.
    endpoints = unlocked.get("/api/settings/git").json()["endpoints"]
    assert [(e["host"], e["method"], e["username"]) for e in endpoints] == [
        ("git.example.org", "https_token", "oauth2")
    ]

    repo = main.PROJECTS_ROOT / "p"
    assert TOKEN not in (repo / ".git" / "config").read_text()
    assert _git(repo, "remote", "get-url", "origin") == url
    for path in ("/api/projects/p/git/status", "/api/dashboard", "/api/settings/git",
                 "/api/projects/p/git/identity", "/api/projects"):
        assert TOKEN not in unlocked.get(path).text, path

    import app.db
    from app.models import GitEndpoint, ProjectActivity

    with app.db.SessionFactory() as s:
        rows = s.query(ProjectActivity).all()
        assert rows, "creation is recorded in the activity feed"
        for row in rows:
            assert TOKEN not in json.dumps([row.kind, row.detail])
        stored = s.query(GitEndpoint).one()
        assert TOKEN.encode() not in stored.ciphertext


def test_a_refused_creation_does_not_keep_the_token(unlocked, tmp_path):
    r = unlocked.post(
        "/api/projects/init",
        json=init_body("p", remote="https://127.0.0.1:9/o/p.git", token=TOKEN),
    )
    assert r.status_code == 400
    assert TOKEN not in r.text
    assert unlocked.get("/api/settings/git").json()["endpoints"] == []


# -- push policy --------------------------------------------------------------


def test_automatic_pushing_publishes_the_first_commits_and_every_save(unlocked, pushes):
    import app.main as main

    remote = bare_remote()
    r = unlocked.post("/api/projects/init", json=init_body("p", remote=remote, push_policy="auto"))
    assert r.status_code == 200, r.text
    repo = main.PROJECTS_ROOT / "p"
    # The first push is part of creation itself, not a background job: it is
    # the one real check that the branch accepts pushes (see init_project).
    assert pushes.jobs == []
    assert _git(remote, "rev-parse", "main") == _git(repo, "rev-parse", "HEAD")

    unlocked.put("/api/projects/p/files/design.md", json={"content": "# board\n"})
    assert pushes.run() == 1
    assert _git(remote, "rev-parse", "main") == _git(repo, "rev-parse", "HEAD")
    st = unlocked.get("/api/projects/p/git/status").json()
    assert st["push_policy"] == "auto"
    assert st["last_push"]["ok"] is True and st["ahead"] == 0


def test_manual_pushing_pushes_nothing_by_itself(unlocked, pushes):
    remote = bare_remote()
    unlocked.post("/api/projects/init", json=init_body("p", remote=remote))
    unlocked.put("/api/projects/p/files/design.md", json={"content": "# board\n"})
    assert pushes.jobs == []
    assert _git(remote, "for-each-ref") == ""


def test_a_failed_automatic_push_is_recorded_and_a_later_push_clears_it(unlocked, pushes):
    import app.main as main

    remote = bare_remote()
    unlocked.post("/api/projects/init", json=init_body("p", remote=remote, push_policy="auto"))
    pushes.run()
    repo = main.PROJECTS_ROOT / "p"

    # Somebody pushed to the remote from elsewhere: the next push is rejected
    # as non-fast-forward, which is exactly the failure nobody should miss.
    other = bare_remote() + "-work"
    _git(Path(remote).parent, "clone", "-q", remote, other)
    Path(other, "x.md").write_text("theirs\n")
    _git(other, "add", "-A")
    subprocess.run(["git", "-c", "user.email=o@example.com", "-c", "user.name=o", "commit", "-qm", "theirs"],
                   cwd=other, check=True)
    _git(other, "push", "-q", "origin", "main")

    r = unlocked.put("/api/projects/p/files/design.md", json={"content": "# mine\n"})
    assert r.json()["committed"] is True, "the commit is not undone by a push failing"
    pushes.run()
    st = unlocked.get("/api/projects/p/git/status").json()
    assert st["last_push"]["ok"] is False
    assert "rejected" in st["last_push"]["error"] or "fetch first" in st["last_push"]["error"]

    # Reconciled by hand (a merge, which is a human's call), then a manual
    # push succeeds and clears the recorded failure.
    _git(repo, "fetch", "-q", remote, "main")
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "merge", "-q", "--no-edit", "FETCH_HEAD"],
        cwd=repo, check=True,
    )
    assert unlocked.post("/api/projects/p/git/push").status_code == 200
    st = unlocked.get("/api/projects/p/git/status").json()
    assert st["last_push"]["ok"] is True and st["last_push"]["error"] == ""


def test_a_failed_manual_push_is_recorded_too(unlocked, tmp_path):
    import shutil

    remote = bare_remote()
    unlocked.post("/api/projects/init", json=init_body("p", remote=remote))
    shutil.rmtree(remote)
    assert unlocked.post("/api/projects/p/git/push").status_code == 400
    st = unlocked.get("/api/projects/p/git/status").json()
    assert st["last_push"]["ok"] is False and st["last_push"]["error"]


def test_concurrent_commits_push_one_after_the_other(unlocked, pushes):
    import app.main as main

    remote = bare_remote()
    unlocked.post("/api/projects/init", json=init_body("p", remote=remote, push_policy="auto"))
    pushes.run()
    unlocked.put("/api/projects/p/files/a.md", json={"content": "a\n"})
    unlocked.put("/api/projects/p/files/b.md", json={"content": "b\n"})
    # Two pushes queued; the origin lock serialises them, and the remote ends
    # at the newest commit whichever ran first.
    import threading

    jobs, pushes.jobs = pushes.jobs, []
    threads = [threading.Thread(target=j) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(jobs) == 2
    assert _git(remote, "rev-parse", "main") == _git(main.PROJECTS_ROOT / "p", "rev-parse", "HEAD")


def test_only_the_owner_changes_the_push_policy(unlocked, second_user):
    unlocked.post("/api/projects/init", json=init_body("p"))
    r = unlocked.put("/api/projects/p/git/push-policy", json={"policy": "auto"})
    assert r.status_code == 200 and r.json()["push_policy"] == "auto" and r.json()["warning"] == ""
    r = unlocked.put("/api/projects/p/git/push-policy", json={"policy": "manual"})
    assert "not zero" in r.json()["warning"]
    assert unlocked.put("/api/projects/p/git/push-policy", json={"policy": None}).status_code == 400

    unlocked.post("/api/projects/p/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]
    second_user.post(f"/api/invitations/{inv}/accept")
    assert second_user.put("/api/projects/p/git/push-policy", json={"policy": "auto"}).status_code == 403


# -- the credential, from project settings ------------------------------------


def test_project_settings_names_the_credential_a_push_would_use(unlocked, monkeypatch):
    import app.main as main
    from app.projects import RemoteProbe

    monkeypatch.setattr(main.projects, "probe_remote", lambda url, env=None: RemoteProbe("ok", True, ""))
    url = "https://git.example.org/o/board.git"
    unlocked.post("/api/projects/init", json=init_body("p", remote=url))
    got = unlocked.get("/api/projects/p/git/credential").json()
    assert got == {"host": "git.example.org", "credential": None, "method": None, "https": True, "problem": ""}

    r = unlocked.put("/api/projects/p/git/credential", json={"token": TOKEN, "username": "me"})
    assert r.status_code == 200 and TOKEN not in r.text
    assert unlocked.get("/api/projects/p/git/credential").json()["credential"] == r.json()["credential"]
    # Into the one store, and a second token for the host replaces the first
    # rather than adding a row the matcher would have to choose between.
    unlocked.put("/api/projects/p/git/credential", json={"token": TOKEN + "-rotated"})
    assert len(unlocked.get("/api/settings/git").json()["endpoints"]) == 1
    assert TOKEN not in (main.PROJECTS_ROOT / "p" / ".git" / "config").read_text()


def test_a_token_is_refused_for_a_local_or_ssh_remote(unlocked):
    unlocked.post("/api/projects/init", json=init_body("p"))
    r = unlocked.put("/api/projects/p/git/credential", json={"token": TOKEN})
    assert r.status_code == 400 and TOKEN not in r.text


class Inline:
    """Runs each job the moment it is submitted — the worst case for timing,
    and the one that shows when a job is submitted before the request's data
    is visible to the session the job records with."""

    def submit(self, fn):
        fn()


def test_the_push_is_started_only_once_the_request_has_committed(unlocked, monkeypatch):
    # The first push of a new project records its outcome on a membership row
    # created in the same request. Started before that request committed, a
    # fast failure found no row and was dropped without a word.
    import shutil

    import app.main as main

    monkeypatch.setattr(main, "AUTOPUSH", Inline())
    remote = bare_remote()
    assert unlocked.post(
        "/api/projects/init", json=init_body("p", remote=remote, push_policy="auto")
    ).status_code == 200
    st = unlocked.get("/api/projects/p/git/status").json()
    assert st["last_push"] is not None and st["last_push"]["ok"] is True

    shutil.rmtree(remote)
    unlocked.put("/api/projects/p/files/design.md", json={"content": "# board\n"})
    st = unlocked.get("/api/projects/p/git/status").json()
    assert st["last_push"]["ok"] is False and st["last_push"]["error"]


# -- cloning asks the same questions ------------------------------------------


@pytest.mark.parametrize(
    "override,why",
    [
        ({"push_policy": None}, "choose a push policy"),
        ({"push_policy": "sometimes"}, "choose a push policy"),
        ({"git_email": "nope"}, "does not look like an address"),
        ({"git_name": ""}, "commit name is required"),
    ],
)
def test_cloning_refuses_without_a_push_policy_or_identity(unlocked, tmp_path, override, why):
    import app.main as main

    remote = _remote_with_history(tmp_path)
    r = unlocked.post("/api/projects/clone", json=clone_body("c", remote, **override))
    assert r.status_code == 400 and why in r.json()["detail"]
    # Refused before git ran: no working copy left without a project row.
    assert not (main.PROJECTS_ROOT / "c").exists()


def test_a_clone_records_its_policy_and_the_cloners_identity(unlocked, tmp_path):
    import app.main as main

    remote = _remote_with_history(tmp_path)
    r = unlocked.post(
        "/api/projects/clone",
        json=clone_body("c", remote, push_policy="auto", git_name="Clo Ner",
                        git_email="clo@example.com", save_as_default=True),
    )
    assert r.status_code == 200, r.text
    assert unlocked.get("/api/projects/c/git/status").json()["push_policy"] == "auto"
    got = unlocked.get("/api/projects/c/git/identity").json()
    assert got["project"] == {"name": "Clo Ner", "email": "clo@example.com"}
    assert got["default"] == {"name": "Clo Ner", "email": "clo@example.com"}

    unlocked.put("/api/projects/c/files/design.md", json={"content": "# mine\n"})
    who = _git(main.PROJECTS_ROOT / "c", "log", "-1", "--format=%an <%ae>")
    assert who == "Clo Ner <clo@example.com>"


def test_a_clone_without_save_as_default_leaves_the_default_alone(unlocked, tmp_path):
    remote = _remote_with_history(tmp_path)
    unlocked.post("/api/projects/clone", json=clone_body("c", remote))
    assert unlocked.get("/api/me/git-identity").json()["identity"]["email"] == "test@example.com"


def test_overlapping_pushes_record_in_the_order_they_ran(unlocked, pushes, monkeypatch):
    """Codex P1 on #19. Two automatic pushes for one member: the first
    succeeds, the second fails. If the first records its success after
    letting go of the origin lock, the second can push, fail and record in
    between, and the late success then hides that the newest commit is
    unpublished. Driven step by step: the first push's record is held open
    until the second has had every chance to run."""
    import threading

    import app.autopush as autopush_mod
    import app.main as main
    from app.projects import ProjectError

    unlocked.post("/api/projects/init", json=init_body("p", remote=bare_remote(), push_policy="auto"))
    pushes.run()
    unlocked.put("/api/projects/p/files/a.md", json={"content": "a\n"})
    unlocked.put("/api/projects/p/files/b.md", json={"content": "b\n"})
    first, second = pushes.jobs
    pushes.jobs = []

    real_push = main.projects._push_locked
    calls = {"n": 0}

    def push_locked(d, url, env):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ProjectError("rejected: the second push failed")
        return real_push(d, url, env)

    monkeypatch.setattr(main.projects, "_push_locked", push_locked)

    real_record = autopush_mod.record
    first_recording = threading.Event()
    release = threading.Event()

    def record(session, project_id, user_id, error):
        if error is None and not first_recording.is_set():
            first_recording.set()
            release.wait(10)
        return real_record(session, project_id, user_id, error)

    monkeypatch.setattr(autopush_mod, "record", record)

    a = threading.Thread(target=first)
    a.start()
    assert first_recording.wait(10)
    b = threading.Thread(target=second)
    b.start()
    b.join(1.5)  # with the fix, b is waiting on the lock a still holds
    release.set()
    a.join(30)
    b.join(30)

    st = unlocked.get("/api/projects/p/git/status").json()
    assert st["last_push"]["ok"] is False, "a stale success overwrote the newer failure"
    assert "second push failed" in st["last_push"]["error"]


@pytest.mark.parametrize(
    "stored_host,method,url",
    [
        # Saved before hosts were normalised: the default port spelled out.
        ("git.example:443", "https_token", "https://git.example/o/p.git"),
        # IPv6 with a port, bracketed as stored; the browser parses it bare.
        ("[2001:db8::1]:8443", "https_token", "https://[2001:db8::1]:8443/o/p.git"),
        ("2001:db8::1", "https_token", "https://[2001:db8::1]/o/p.git"),
    ],
)
def test_the_credential_for_a_remote_is_matched_the_way_a_push_matches_it(
    unlocked, stored_host, method, url
):
    # Codex P2 on #19: the dialog matched hosts itself, by exact spelling, and
    # asked for a new token where the push would have used the saved one.
    import app.db
    from sqlalchemy import select

    from app.models import GitEndpoint, User

    with app.db.SessionFactory() as s:
        user = s.scalar(select(User).where(User.clerk_user_id == "user_test"))
        s.add(GitEndpoint(user_id=user.id, name="legacy", host=stored_host, method=method,
                          username="git", nonce=b"0" * 12, ciphertext=b"x"))
        s.commit()
    got = unlocked.post("/api/projects/credential-for", json={"url": url}).json()
    assert got["credential"] == "legacy" and got["https"] is True


def test_a_half_typed_url_has_no_credential_and_no_error(unlocked):
    r = unlocked.post("/api/projects/credential-for", json={"url": "https://"})
    assert r.status_code == 200 and r.json()["credential"] is None
