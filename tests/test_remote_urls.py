"""Remote URLs a user can name: what git may be pointed at, and who may point it.

`POST /api/projects/clone` passed the user's URL straight to `git clone`, so any
signed-in user could clone a *local path* — another project's repository on the
server's own disk, or the shared modules repository — into a project they own,
and the URL was a positional argument git would also read as an option. One
check now decides what a remote may be, for cloning and for setting a remote,
and git is told the same transports through GIT_ALLOW_PROTOCOL.
"""

from __future__ import annotations

import subprocess

import pytest

from app import projects as projects_mod
from app.projects import ProjectError, check_remote_url


@pytest.fixture
def deployment_protocols(monkeypatch):
    """What a deployment allows. The suite enables file:// for its fixtures."""
    monkeypatch.setenv("BLPL_GIT_ALLOWED_PROTOCOLS", "https:ssh")


def _repo(path):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    (path / "secret.md").write_text("another project's design\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
        cwd=path, check=True,
    )
    return path


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/CK-Consulting/BLPL.git",
        "ssh://git@github.com/CK-Consulting/BLPL.git",
        "git@github.com:CK-Consulting/BLPL.git",
    ],
)
def test_real_remotes_are_accepted(deployment_protocols, url):
    assert check_remote_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "/app/data/projects/someone-elses-board",
        "file:///app/data/projects/someone-elses-board",
        "../someone-elses-board",
        "ext::sh -c touch% /tmp/x",
        "--upload-pack=touch /tmp/x",
        "http://example.com/plain-text.git",
        "https://github.com/a b.git",
        "",
    ],
)
def test_anything_else_is_refused(deployment_protocols, url):
    with pytest.raises(ProjectError):
        check_remote_url(url)


def test_cloning_another_project_off_the_disk_is_refused(unlocked, deployment_protocols, tmp_path):
    victim = _repo(tmp_path / "victim")
    for remote in (str(victim), f"file://{victim}"):
        r = unlocked.post(
            "/api/projects/clone", json={"name": "grab", "remote": remote, "branch": "main"}
        )
        assert r.status_code == 400, r.text
    import app.main as main

    assert not (main.PROJECTS_ROOT / "grab").exists()


def test_git_itself_refuses_a_disallowed_transport(deployment_protocols, tmp_path):
    # Defence in depth: a file:// remote that reached a project's config some
    # other way still cannot be pulled from, because git is told the allow-list.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    subprocess.run(["git", "remote", "add", "origin", f"file://{_repo(tmp_path / 'v')}"], cwd=work, check=True)
    subprocess.run(["git", "branch", "--set-upstream-to=origin/main"], cwd=work, capture_output=True)
    with pytest.raises(ProjectError):
        store.pull("p")


# -- setting a remote from the Git panel -------------------------------------


def _project(client, name="p"):
    assert client.post("/api/projects/init", json={"name": name}).status_code == 200


def test_the_owner_sets_a_remote_and_status_shows_it(unlocked):
    _project(unlocked)
    r = unlocked.put("/api/projects/p/git/remote", json={"url": "https://github.com/o/p.git"})
    assert r.status_code == 200, r.text
    st = unlocked.get("/api/projects/p/git/status").json()
    assert st["has_remote"] and st["remote_url"] == "https://github.com/o/p.git"

    # …and changes it.
    unlocked.put("/api/projects/p/git/remote", json={"url": "git@github.com:o/q.git"})
    assert unlocked.get("/api/projects/p/git/status").json()["remote_url"] == "git@github.com:o/q.git"


def test_a_local_path_is_refused_as_a_remote(unlocked, deployment_protocols, tmp_path):
    _project(unlocked)
    r = unlocked.put("/api/projects/p/git/remote", json={"url": str(_repo(tmp_path / "v"))})
    assert r.status_code == 400
    assert not unlocked.get("/api/projects/p/git/status").json()["has_remote"]


def test_a_member_who_is_not_the_owner_cannot_set_it(unlocked, second_user):
    _project(unlocked, "mine")
    unlocked.post("/api/projects/mine/members", json={"email": "other@example.com"})
    inv = second_user.get("/api/invitations").json()[0]["id"]
    second_user.post(f"/api/invitations/{inv}/accept")

    r = second_user.put("/api/projects/mine/git/remote", json={"url": "https://github.com/o/p.git"})
    assert r.status_code == 403


def test_a_malformed_url_is_a_validation_error_not_a_crash(deployment_protocols):
    # Codex P2 on #10: urlsplit raises ValueError on an unclosed IPv6 bracket,
    # which the routes (expecting ProjectError) turned into a 500.
    with pytest.raises(ProjectError):
        check_remote_url("https://[bad/repo")


def _bare(tmp_path, name, src):
    bare = tmp_path / f"{name}.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


def _heads(bare):
    out = subprocess.run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
                         cwd=bare, capture_output=True, text=True).stdout
    return set(out.split())


def test_the_projects_remote_is_origin_whatever_the_branch_tracks(tmp_path):
    # Codex P1s on #10, three rounds: the remote shown, the host a credential
    # was leased for and the place a push went could each be resolved
    # differently (the branch's upstream, pushRemote, pushDefault, a member's
    # own branch). The app now names origin in every pull and push.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    other = _bare(tmp_path, "other", work)
    subprocess.run(["git", "remote", "add", "upstream", f"file://{other}"], cwd=work, check=True)
    subprocess.run(["git", "fetch", "-q", "upstream"], cwd=work, check=True)
    subprocess.run(["git", "branch", "-q", "--set-upstream-to=upstream/main"], cwd=work, check=True)

    origin = _bare(tmp_path, "origin", work)
    store.set_remote(work, f"file://{origin}")

    st = store.status("p", checkout=work)
    assert (st.remote_name, st.remote_url) == ("origin", f"file://{origin}")
    assert store.remote_of("p") == f"file://{origin}"


def test_pushes_go_to_origin_despite_push_overrides_and_from_members(tmp_path):
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    origin = _bare(tmp_path, "origin", work)
    decoy = _bare(tmp_path, "decoy", work)
    subprocess.run(["git", "remote", "add", "origin", f"file://{origin}"], cwd=work, check=True)
    subprocess.run(["git", "remote", "add", "decoy", f"file://{decoy}"], cwd=work, check=True)
    # Everything git offers for sending an argument-less push somewhere else.
    subprocess.run(["git", "config", "remote.pushDefault", "decoy"], cwd=work, check=True)
    subprocess.run(["git", "config", "branch.main.pushRemote", "decoy"], cwd=work, check=True)

    # The owner's branch tracks origin — so an argument-less push is the one
    # pushRemote / pushDefault redirect. A new commit shows where it went.
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=work, check=True)
    subprocess.run(["git", "branch", "-q", "--set-upstream-to=origin/main"], cwd=work, check=True)
    (work / "more.md").write_text("more\n")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "more"], cwd=work, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True).stdout.strip()

    store.push("p")
    tip = lambda bare: subprocess.run(["git", "rev-parse", "main"], cwd=bare, capture_output=True, text=True).stdout.strip()
    assert tip(origin) == head and tip(decoy) != head

    member = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "user/2", str(member)], cwd=work, check=True)
    subprocess.run(["git", "config", "branch.user/2.pushRemote", "decoy"], cwd=work, check=True)
    store.push("p", checkout=member)
    assert "user/2" in _heads(origin) and "user/2" not in _heads(decoy)
    # …and the credential would be chosen for exactly that URL.
    assert store.remote_of("p", checkout=member) == f"file://{origin}"


def test_with_no_remote_at_all_origin_is_added(tmp_path):
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    store.set_remote(work, "git@github.com:o/p.git")
    assert subprocess.run(["git", "remote"], cwd=work, capture_output=True, text=True).stdout.split() == ["origin"]


def test_a_token_in_the_url_is_refused_not_stored(deployment_protocols):
    # Codex P1 on #10: accepted as-is, the token was written to .git/config in
    # plaintext; the panel only hid it when displaying.
    with pytest.raises(ProjectError) as exc:
        check_remote_url("https://user:ghp_secret@github.com/o/p.git")
    assert "token" in str(exc.value)
    assert check_remote_url("https://user@github.com/o/p.git")  # a username alone is fine


def test_changing_a_remote_drops_its_separate_push_url(tmp_path):
    # Codex P1 on #10: a pushurl kept pushes going to the old destination while
    # the panel reported the new one.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    subprocess.run(["git", "remote", "add", "origin", "https://old.example/o/p.git"], cwd=work, check=True)
    subprocess.run(["git", "remote", "set-url", "--push", "origin", "https://old-push.example/o/p.git"], cwd=work, check=True)

    store.set_remote(work, "https://github.com/o/new.git")
    push = subprocess.run(["git", "remote", "get-url", "--push", "origin"], cwd=work, capture_output=True, text=True).stdout.strip()
    assert push == "https://github.com/o/new.git"


def test_cloning_a_malformed_url_is_a_400(unlocked):
    # Codex P2 on #10: credentials were looked up (parsing the URL) before it
    # was validated, so this was still a 500.
    r = unlocked.post("/api/projects/clone", json={"name": "x", "remote": "https://[bad/repo", "branch": "main"})
    assert r.status_code == 400, r.text
