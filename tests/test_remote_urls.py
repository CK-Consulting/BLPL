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
import threading

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
    assert store.remote_of("p", operation="push") == f"file://{origin}"


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
    assert store.remote_of("p", operation="push", checkout=member) == f"file://{origin}"


def test_pull_and_push_credentials_follow_their_own_urls(tmp_path):
    # Codex P1 on #10: remote_of answered with the push URL for everything, so
    # with origin fetching from host A and pushing to host B, a pull fetched
    # from A while the credential helper offered it B's token — leaking the
    # token to the wrong host and failing the pull.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    subprocess.run(["git", "remote", "add", "origin", "https://host-a.example/o/p.git"], cwd=work, check=True)
    subprocess.run(["git", "remote", "set-url", "--push", "origin", "https://host-b.example/o/p.git"], cwd=work, check=True)

    assert store.remote_of("p", operation="fetch") == "https://host-a.example/o/p.git"
    assert store.remote_of("p", operation="push") == "https://host-b.example/o/p.git"
    with pytest.raises(ValueError):
        store.remote_of("p", operation="both")


def test_a_never_pushed_branch_still_has_something_to_push(tmp_path):
    # Codex P1 on #10: after adding origin to a clean local project there is
    # no upstream, so ahead stayed 0 and the clean tree disabled Commit + Push
    # — the only way that history could ever reach the new remote.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    store.set_remote(work, f"file://{origin}")

    st = store.status("p", checkout=work)
    assert st.unpublished and st.ahead == 1 and not st.dirty

    store.push("p")
    st = store.status("p", checkout=work)
    assert not st.unpublished and st.ahead == 0


def test_moving_origin_to_an_empty_repository_reads_as_unpublished(tmp_path):
    # Codex P1 on #10, second round: after "Change remote…" from a pushed
    # repository A to an empty B, the upstream and refs/remotes/origin/* from A
    # remained, so status counted against A's refs (unpublished false, ahead
    # 0) and the clean tree disabled the push B was waiting for.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    a = tmp_path / "a.git"
    subprocess.run(["git", "init", "-q", "--bare", str(a)], check=True)
    store.set_remote(work, f"file://{a}")
    store.push("p")
    assert not store.status("p", checkout=work).unpublished

    b = tmp_path / "b.git"
    subprocess.run(["git", "init", "-q", "--bare", str(b)], check=True)
    store.set_remote(work, f"file://{b}")

    st = store.status("p", checkout=work)
    assert st.unpublished and st.ahead == 1
    store.push("p")
    assert "main" in _heads(b)
    st = store.status("p", checkout=work)
    assert not st.unpublished and st.ahead == 0


def test_publication_is_measured_against_origin_not_the_tracked_remote(tmp_path):
    # Same round: a branch tracking another remote was measured against that
    # remote, while every push goes to origin by name.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    other = _bare(tmp_path, "other", work)
    subprocess.run(["git", "remote", "add", "upstream", f"file://{other}"], cwd=work, check=True)
    subprocess.run(["git", "fetch", "-q", "upstream"], cwd=work, check=True)
    subprocess.run(["git", "branch", "-q", "--set-upstream-to=upstream/main"], cwd=work, check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    store.set_remote(work, f"file://{origin}")

    st = store.status("p", checkout=work)
    assert st.unpublished and st.ahead == 1


def test_with_no_remote_at_all_origin_is_added(tmp_path):
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    store.set_remote(work, "git@github.com:o/p.git")
    assert subprocess.run(["git", "remote"], cwd=work, capture_output=True, text=True).stdout.split() == ["origin"]


_SECRET_BEARING = [
    # urlsplit raises on each of these before the password check runs, so the
    # refusal used to quote the first 60 characters, token and all.
    ("https://user:ghp_tokensecret@[bad/repo", "ghp_tokensecret"),
    ("ssh://u:pa55secret@[::1/x", "pa55secret"),
    # A password containing an @: redaction that stopped at the first one
    # showed the rest.
    ("https://user:tok@ensecret@host/x", "ensecret"),
    # Long enough that cutting to 60 characters before redacting drops the @.
    ("https://user:" + "s" * 70 + "@[bad/repo", "s" * 20),
    # Unprintable characters get the "not a remote URL" refusal.
    ("https://user:ghp_tokensecret@host/o p.git", "ghp_tokensecret"),
    # A credential where the scheme should be.
    ("me:ghp_tokensecret@host://x", "ghp_tokensecret"),
    # And the scp-like form, which has no scheme to anchor on.
    ("me:ghp_tokensecret@host", "ghp_tokensecret"),
]


@pytest.mark.parametrize("url,secret", _SECRET_BEARING)
def test_a_refused_url_is_never_quoted_with_its_password(deployment_protocols, url, secret):
    # Codex P2 on #10: `https://user:token@[bad/repo` failed in urlsplit and
    # the error echoed the raw URL to the API client, token included.
    with pytest.raises(ProjectError) as exc:
        check_remote_url(url)
    assert secret not in str(exc.value)


def test_redaction_takes_a_password_containing_an_at_whole():
    # Same finding: stopping at the first @ left `ensecret` behind.
    url = "https://user:tok@ensecret@host/x"
    assert projects_mod._redact_text(f"git: {url} failed") == "git: https://user:***@host/x failed"
    assert projects_mod._redact_url(url) == "https://user:***@host/x"


def test_the_remote_route_never_echoes_a_password(unlocked, deployment_protocols):
    _project(unlocked)
    for url, secret in _SECRET_BEARING:
        r = unlocked.put("/api/projects/p/git/remote", json={"url": url})
        assert r.status_code == 400, (url, r.text)
        assert secret not in r.text, url
    r = unlocked.post("/api/projects/clone", json={
        "name": "x", "remote": "https://user:ghp_tokensecret@[bad/repo", "branch": "main",
    })
    assert r.status_code == 400 and "ghp_tokensecret" not in r.text


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


def _config(work, *args):
    subprocess.run(["git", "config", *args], cwd=work, check=True)


def test_two_push_urls_are_refused_before_any_credential(tmp_path):
    # Codex P1 on #10: get-url answers with the first push URL only, while git
    # push contacts every one, so a credential leased for host A was offered
    # to host B.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    subprocess.run(["git", "remote", "add", "origin", "https://host-a.example/o/p.git"], cwd=work, check=True)
    _config(work, "--add", "remote.origin.pushurl", "https://host-a.example/o/p.git")
    _config(work, "--add", "remote.origin.pushurl", "https://host-b.example/o/p.git")

    with pytest.raises(ProjectError, match="Set a single URL in Project settings"):
        store.remote_of("p", operation="push")
    with pytest.raises(ProjectError, match="2 push URLs"):
        store.push("p")
    # The fetch side has one URL and is unaffected.
    assert store.remote_of("p", operation="fetch") == "https://host-a.example/o/p.git"


def test_two_urls_and_no_push_url_refuse_both_directions(tmp_path):
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    subprocess.run(["git", "remote", "add", "origin", "https://host-a.example/o/p.git"], cwd=work, check=True)
    _config(work, "--add", "remote.origin.url", "https://host-b.example/o/p.git")

    for op in ("fetch", "push"):
        with pytest.raises(ProjectError, match="Set a single URL"):
            store.remote_of("p", operation=op)
    with pytest.raises(ProjectError, match="Set a single URL"):
        store.push("p")
    with pytest.raises(ProjectError, match="Set a single URL"):
        store.pull("p")


def test_setting_the_remote_leaves_exactly_one_url(tmp_path):
    # set-url alone replaces only the first url, so the second would stay a
    # second destination for every push.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    subprocess.run(["git", "remote", "add", "origin", "https://host-a.example/o/p.git"], cwd=work, check=True)
    _config(work, "--add", "remote.origin.url", "https://host-b.example/o/p.git")

    store.set_remote(work, "https://github.com/o/new.git")
    urls = subprocess.run(
        ["git", "remote", "get-url", "--all", "--push", "origin"],
        cwd=work, capture_output=True, text=True,
    ).stdout.split()
    assert urls == ["https://github.com/o/new.git"]
    assert store.remote_of("p", operation="push") == "https://github.com/o/new.git"


def test_a_remote_changed_after_leasing_is_refused_not_used(tmp_path):
    # Codex P1 on #10: the route leased a credential for origin's URL, then
    # push named origin and git re-read the config, so an owner changing the
    # remote in between sent the member's token for host A to host B. Git is
    # handed the resolved URL, and a URL origin no longer has is refused.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    a = tmp_path / "a.git"
    b = tmp_path / "b.git"
    for bare in (a, b):
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    store.set_remote(work, f"file://{a}")

    leased_for = store.remote_of("p", operation="push")
    store.set_remote(work, f"file://{b}")          # the owner, mid-request
    with pytest.raises(ProjectError, match="remote changed"):
        store.push("p", url=leased_for)
    assert _heads(a) == set() and _heads(b) == set()

    # Resolved and used together, it goes where the credential was chosen for,
    # and the bookkeeping --set-upstream used to do is still done.
    store.push("p", url=store.remote_of("p", operation="push"))
    assert "main" in _heads(b)
    st = store.status("p", checkout=work)
    assert not st.unpublished and st.ahead == 0


def test_pull_uses_the_url_it_was_given(tmp_path):
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    a = tmp_path / "a.git"
    subprocess.run(["git", "init", "-q", "--bare", str(a)], check=True)
    store.set_remote(work, f"file://{a}")
    store.push("p")
    leased_for = store.remote_of("p", operation="fetch")
    store.pull("p", url=leased_for)
    store.set_remote(work, "https://elsewhere.example/o/p.git")
    with pytest.raises(ProjectError, match="remote changed"):
        store.pull("p", url=leased_for)


def _commit_file(work, name):
    (work / name).write_text(f"{name}\n")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name],
                   cwd=work, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work,
                          capture_output=True, text=True).stdout.strip()


def test_pull_after_changing_remote_fast_forwards_without_an_upstream(tmp_path):
    # Codex P1 on #10: "Change remote…" forgets the upstream, and pull refused
    # without one, so an origin holding a fast-forward of HEAD could be neither
    # pulled (no upstream) nor pushed to (remote ahead).
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    a = tmp_path / "a.git"
    subprocess.run(["git", "init", "-q", "--bare", str(a)], check=True)
    store.set_remote(work, f"file://{a}")
    store.push("p")

    # B is this history plus one commit someone else pushed there.
    b = _bare(tmp_path, "b", work)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(b), str(other)], check=True)
    ahead_tip = _commit_file(other, "theirs.md")
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=other, check=True)

    store.set_remote(work, f"file://{b}")
    store.pull("p", url=store.remote_of("p", operation="fetch"))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work,
                          capture_output=True, text=True).stdout.strip()
    assert head == ahead_tip
    st = store.status("p", checkout=work)
    assert not st.unpublished and (st.ahead, st.behind) == (0, 0)
    upstream = subprocess.run(["git", "rev-parse", "--abbrev-ref", "@{upstream}"], cwd=work,
                              capture_output=True, text=True).stdout.strip()
    assert upstream == "origin/main"

    mine = _commit_file(work, "mine.md")
    store.push("p")
    tip = subprocess.run(["git", "rev-parse", "main"], cwd=b,
                         capture_output=True, text=True).stdout.strip()
    assert tip == mine


def test_pulling_a_branch_origin_does_not_have_says_so(tmp_path):
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    empty = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", str(empty)], check=True)
    store.set_remote(work, f"file://{empty}")
    with pytest.raises(ProjectError, match="has no branch 'main' to pull from"):
        store.pull("p")


def test_the_several_urls_refusal_never_shows_a_token(tmp_path):
    # Codex P1 on #10: a hand-edited config can still hold a token in a URL,
    # and the refusal listed every URL to any member who pressed Pull.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    subprocess.run(
        ["git", "remote", "add", "origin", "https://me:ghp_secret123@host-a.example/o/p.git"],
        cwd=work, check=True,
    )
    _config(work, "--add", "remote.origin.url", "https://host-b.example/o/p.git")
    with pytest.raises(ProjectError) as exc:
        store.pull("p")
    assert "ghp_secret123" not in str(exc.value)
    assert "host-a.example" in str(exc.value)


def test_cloning_a_malformed_url_is_a_400(unlocked):
    # Codex P2 on #10: credentials were looked up (parsing the URL) before it
    # was validated, so this was still a 500.
    r = unlocked.post("/api/projects/clone", json={"name": "x", "remote": "https://[bad/repo", "branch": "main"})
    assert r.status_code == 400, r.text


def test_a_token_already_in_origin_is_refused_before_git_sees_it(tmp_path):
    # Codex P1 on #10: the setter refuses a URL with a token, but a repository
    # configured before it (or edited by hand) still handed that URL to git as
    # an argument — on the command line for anything on the host to read, and
    # back to every member in the error when the operation failed.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    subprocess.run(
        ["git", "remote", "add", "origin", "https://me:ghp_secret123@host-a.example/o/p.git"],
        cwd=work, check=True,
    )
    for op in ("fetch", "push"):
        with pytest.raises(ProjectError, match="password or token") as exc:
            store.remote_of("p", operation=op)
        assert "ghp_secret123" not in str(exc.value)
    for call in (store.pull, store.push):
        with pytest.raises(ProjectError, match="password or token") as exc:
            call("p")
        assert "ghp_secret123" not in str(exc.value)

    # A username alone is not a secret and still resolves.
    _config(work, "remote.origin.url", "https://me@host-a.example/o/p.git")
    assert store.remote_of("p", operation="push") == "https://me@host-a.example/o/p.git"


def test_a_failing_git_command_never_repeats_a_token(tmp_path):
    # Defence in depth for the same finding: _git's error quotes every argument
    # and git's stderr, and both can carry a URL with a token in it.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    with pytest.raises(ProjectError) as exc:
        store._git(work, "rev-parse", "--verify", "https://me:ghp_secret123@host-a.example/o/p.git")
    assert "ghp_secret123" not in str(exc.value)
    assert "https://me:***@host-a.example" in str(exc.value)


def test_saving_the_fetch_url_over_a_separate_push_url_forgets_the_push_side(tmp_path):
    # Codex P2 on #10: origin fetched from A and pushed to B. Saving A dropped
    # the pushurl, moving every push from B to A, but the old URL was read
    # without --push, compared equal, and origin/main from B survived — so
    # status said published, nothing ahead, with A holding none of it.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    a = tmp_path / "a.git"
    b = tmp_path / "b.git"
    for bare in (a, b):
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", f"file://{a}"], cwd=work, check=True)
    subprocess.run(["git", "remote", "set-url", "--push", "origin", f"file://{b}"], cwd=work, check=True)
    store.push("p")
    assert "main" in _heads(b) and not store.status("p", checkout=work).unpublished

    store.set_remote(work, f"file://{a}")

    st = store.status("p", checkout=work)
    assert st.unpublished and st.ahead == 1
    store.push("p")
    assert "main" in _heads(a)


def test_changing_the_remote_mid_push_cannot_be_undone_by_the_push(tmp_path):
    # Codex P1 on #10: push validated origin (A), pushed, then recreated
    # refs/remotes/origin/main. The owner switching origin to an empty B while
    # the push was in flight had set_remote clear that ref in between, and the
    # push wrote it back — so status read "published, ahead 0" against B, which
    # held nothing, and the clean tree disabled the push B was waiting for.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    a = tmp_path / "a.git"
    b = tmp_path / "b.git"
    for bare in (a, b):
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    store.set_remote(work, f"file://{a}")

    real_git = store._git
    owner = {}

    def change_remote():
        try:
            store.set_remote(work, f"file://{b}")
        except Exception as exc:  # reported below, not swallowed
            owner["error"] = exc

    def git_with_owner_mid_push(cwd, *args, **kw):
        if args[:1] == ("push",) and "thread" not in owner:
            # The owner saves a new remote while this push is under way. Given
            # a second to land: without the lock it does, before the push.
            owner["thread"] = threading.Thread(target=change_remote)
            owner["thread"].start()
            owner["thread"].join(timeout=1.0)
        return real_git(cwd, *args, **kw)

    store._git = git_with_owner_mid_push
    store.push("p", url=store.remote_of("p", operation="push"))
    owner["thread"].join(timeout=30)
    store._git = real_git

    assert "error" not in owner
    assert store.remote_of("p", operation="push") == f"file://{b}"
    assert "main" in _heads(a) and _heads(b) == set()
    st = store.status("p", checkout=work)
    assert st.unpublished and st.ahead == 1


def test_changing_the_remote_while_a_member_pushes_says_so(tmp_path, monkeypatch):
    # The same lock from the other side: origin is shared by every worktree,
    # so a member's push in progress holds it for the owner too, and a change
    # that cannot wait it out is a sentence rather than a hang or a race.
    store = projects_mod.Projects(tmp_path / "projects")
    work = _repo(tmp_path / "projects" / "p")
    store.set_remote(work, "https://github.com/o/old.git")
    member = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "user/2", str(member)], cwd=work, check=True)
    monkeypatch.setattr(store, "SET_REMOTE_LOCK_WAIT", 0.2)

    with store._remote_lock(member, 1.0, "busy"):
        with pytest.raises(ProjectError, match="in progress"):
            store.set_remote(work, "https://github.com/o/new.git")
    assert store.remote_of("p", operation="push") == "https://github.com/o/old.git"

    store.set_remote(work, "https://github.com/o/new.git")
    assert store.remote_of("p", operation="push") == "https://github.com/o/new.git"
