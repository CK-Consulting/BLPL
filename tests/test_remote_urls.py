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


def test_changing_the_remote_changes_the_one_the_branch_tracks(tmp_path):
    # Codex P1 on #10: with only a non-origin remote, tracked by the branch,
    # set_remote added a new origin. The panel then showed it while pull and
    # push kept using the old upstream, and credentials were leased for origin.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    bare = tmp_path / "upstream.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "upstream", f"file://{bare}"], cwd=work, check=True)
    subprocess.run(["git", "fetch", "-q", "upstream"], cwd=work, check=True)
    subprocess.run(["git", "branch", "-q", "--set-upstream-to=upstream/main"], cwd=work, check=True)

    store.set_remote(work, "https://github.com/o/new.git")

    remotes = subprocess.run(["git", "remote"], cwd=work, capture_output=True, text=True).stdout.split()
    assert remotes == ["upstream"]
    st = store.status("p", checkout=work)
    assert (st.remote_name, st.remote_url) == ("upstream", "https://github.com/o/new.git")
    assert store.remote_of("p") == "https://github.com/o/new.git"


def test_with_no_remote_at_all_origin_is_added(tmp_path):
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    store.set_remote(work, "git@github.com:o/p.git")
    assert subprocess.run(["git", "remote"], cwd=work, capture_output=True, text=True).stdout.split() == ["origin"]
