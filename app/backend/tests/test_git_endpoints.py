"""Per-user git credentials: stored sealed, matched by host, never echoed back.

The clone dialog used to promise "the deploy's git credentials" — credentials
with no storage and no way to exist. These are their replacement, shaped like
provider keys for the same reasons: per-user, sealed under the user's master
key, plaintext only on its way into a subprocess.
"""

from __future__ import annotations

import pytest

from app import gitstore


# -- host matching, the part that picks a credential from a pasted URL --------


@pytest.mark.parametrize("remote,host", [
    ("https://github.com/acme/widget.git", "github.com"),
    ("https://GitLab.com/acme/widget.git", "gitlab.com"),
    ("ssh://git@git.corp.example:2222/acme/widget.git", "git.corp.example"),
    ("git@github.com:acme/widget.git", "github.com"),
])
def test_the_host_is_read_from_every_remote_shape(remote, host) -> None:
    assert gitstore.host_of(remote) == host


@pytest.mark.parametrize("remote", [
    "/srv/git/widget.git", "./widget", "../elsewhere/widget", "file:///srv/git/widget.git", "",
])
def test_a_local_remote_has_no_host_and_matches_no_credential(remote) -> None:
    """A directory named github.com must not borrow the GitHub token."""
    assert gitstore.host_of(remote) is None


# -- the API surface ----------------------------------------------------------


def test_a_stored_credential_lists_without_its_secret(unlocked) -> None:
    r = unlocked.put("/api/settings/git/github", json={
        "host": "github.com", "method": "https_token",
        "secret": "ghp_SECRETSECRET", "username": "shaun",
    })
    assert r.status_code == 200

    listing = unlocked.get("/api/settings/git").json()
    (row,) = listing["endpoints"]
    assert row["name"] == "github" and row["host"] == "github.com"
    assert "ghp_" not in str(listing), "the secret must never reach a response body"


def test_presets_are_offered_for_the_form(unlocked) -> None:
    listing = unlocked.get("/api/settings/git").json()
    names = {p["name"] for p in listing["presets"]}
    assert {"github", "gitlab"} <= names


def test_https_without_a_username_is_refused_with_the_reason(unlocked) -> None:
    """A bare token fails basic auth in a way that reads as a wrong token, so
    the mistake is caught where it is made."""
    r = unlocked.put("/api/settings/git/github", json={
        "host": "github.com", "method": "https_token", "secret": "tok",
    })
    assert r.status_code == 400 and "username" in r.json()["detail"]


def test_a_host_carrying_a_token_is_refused_without_echoing_it(unlocked) -> None:
    """Codex P2 on #10: a pasted `https://user:token@host` keeps its userinfo
    once the scheme is stripped, and the refusal quoted it back."""
    r = unlocked.put("/api/settings/git/github", json={
        "host": "https://me:ghp_tokensecret@git hub.com", "method": "https_token",
        "secret": "tok", "username": "me",
    })
    assert r.status_code == 400 and "ghp_tokensecret" not in r.text
    # Without the space it used to be stored: the token in the host column.
    r = unlocked.put("/api/settings/git/github", json={
        "host": "https://me:ghp_tokensecret@github.com", "method": "https_token",
        "secret": "tok", "username": "me",
    })
    assert r.status_code == 400 and "ghp_tokensecret" not in r.text
    assert "ghp_tokensecret" not in unlocked.get("/api/settings/git").text


def test_a_host_carrying_a_query_is_refused_without_echoing_it(unlocked) -> None:
    """Codex P1 on #10, the query form: `host?access_token=...` survived the cut
    at the first / and was stored as the host."""
    for host in ("github.com?access_token=SECRETQ", "https://github.com#SECRETQ"):
        r = unlocked.put("/api/settings/git/github", json={
            "host": host, "method": "https_token", "secret": "tok", "username": "me",
        })
        assert r.status_code == 400 and "SECRETQ" not in r.text, host
    assert "SECRETQ" not in unlocked.get("/api/settings/git").text


def test_rotation_replaces_in_place(unlocked) -> None:
    for secret in ("tok-one", "tok-two"):
        unlocked.put("/api/settings/git/github", json={
            "host": "github.com", "method": "https_token",
            "secret": secret, "username": "x",
        })
    assert len(unlocked.get("/api/settings/git").json()["endpoints"]) == 1


def test_deleting_removes_the_row(unlocked) -> None:
    unlocked.put("/api/settings/git/gone", json={
        "host": "example.com", "method": "https_token", "secret": "t", "username": "u",
    })
    assert unlocked.delete("/api/settings/git/gone").json()["removed"] is True
    assert unlocked.get("/api/settings/git").json()["endpoints"] == []


def test_one_users_credentials_are_invisible_to_another(unlocked, second_user) -> None:
    unlocked.put("/api/settings/git/github", json={
        "host": "github.com", "method": "https_token", "secret": "mine", "username": "me",
    })
    assert second_user.get("/api/settings/git").json()["endpoints"] == []


# -- the lease ----------------------------------------------------------------


def test_an_https_lease_puts_the_token_in_the_environment_not_the_command(unlocked) -> None:
    """The token rides env vars read by an inline credential helper — never the
    URL, which git writes into .git/config for the life of the working copy."""
    import app.main as main
    from app.db import SessionFactory
    from sqlalchemy import select
    from app.models import GitEndpoint, User

    unlocked.put("/api/settings/git/github", json={
        "host": "github.com", "method": "https_token",
        "secret": "ghp_token123", "username": "shaun",
    })
    with SessionFactory() as session:
        user = session.scalar(select(User))
        row = session.scalar(select(GitEndpoint).where(GitEndpoint.user_id == user.id))
        from app import userkey, unlock as unlock_mod
        # The master key for the test session: reach it the way routes do is
        # heavier than needed here — decrypt directly with the vault instead.
        assert row.ciphertext != b"ghp_token123"
        assert b"ghp_token123" not in row.ciphertext


def test_an_ssh_lease_is_briefly_a_file_and_then_is_not(tmp_path) -> None:
    from types import SimpleNamespace
    from app import vault

    key = b"k" * 32
    nonce, ct = vault.encrypt_secret(key, "git-endpoint:corp", "-----BEGIN KEY-----")
    row = SimpleNamespace(name="corp", method="ssh_key", username=None, nonce=nonce, ciphertext=ct)

    captured = {}
    with gitstore.lease(key, row) as cred:
        path = cred.env["GIT_SSH_COMMAND"].split(" -i ", 1)[1].split(" ", 1)[0]
        captured["path"] = path
        import os, stat
        st = os.stat(path)
        assert stat.S_IMODE(st.st_mode) == 0o600
        assert "BEGIN KEY" in open(path).read()

    import os
    assert not os.path.exists(captured["path"]), "the key file must not outlive the lease"


def test_an_https_lease_reads_back_the_token_it_sealed() -> None:
    from types import SimpleNamespace
    from app import vault

    key = b"m" * 32
    nonce, ct = vault.encrypt_secret(key, "git-endpoint:gh", "ghp_abc")
    row = SimpleNamespace(name="gh", method="https_token", username="shaun", nonce=nonce, ciphertext=ct)

    with gitstore.lease(key, row) as cred:
        assert cred.env["GIT_HTTP_TOKEN"] == "ghp_abc"
        assert cred.env["GIT_HTTP_USER"] == "shaun"
        assert cred.env["GIT_TERMINAL_PROMPT"] == "0"
        # Configured helpers are reset before ours is installed, so a system
        # helper cannot answer first with the wrong identity.
        assert cred.env["GIT_CONFIG_VALUE_0"] == ""



def test_the_https_helper_answers_only_for_its_own_host(tmp_path) -> None:
    """Codex P1 on #10: the helper answered every request, so whichever host
    git ended up talking to was handed this token. It now answers `get` for
    the host the credential was stored for and nothing else."""
    import os
    import subprocess
    from types import SimpleNamespace
    from app import vault

    key = b"h" * 32
    nonce, ct = vault.encrypt_secret(key, "git-endpoint:gh", "ghp_scoped")
    row = SimpleNamespace(
        name="gh", method="https_token", username="me", host="github.com",
        nonce=nonce, ciphertext=ct,
    )

    def fill(host: str, env: dict) -> str:
        r = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost={host}\n\n", capture_output=True, text=True,
            env={**os.environ, **env, "HOME": str(tmp_path)}, cwd=tmp_path,
        )
        return r.stdout

    with gitstore.lease(key, row) as cred:
        assert "password=ghp_scoped" in fill("github.com", cred.env)
        assert "password=ghp_scoped" in fill("GitHub.com:443", cred.env)
        assert "ghp_scoped" not in fill("evil.example", cred.env)


@pytest.mark.parametrize("stored", ["2001:db8::1", "[2001:DB8::1]"])
def test_the_https_helper_reads_a_bracketed_ipv6_host_whole(tmp_path, stored) -> None:
    """Codex P2 on #10: the port was cut at the first colon, so git's
    `[2001:db8::1]:8443` became `[2001` — never matching the stored host, and
    matching any other address that began `[2001`. host_of drops the brackets,
    so a row may hold either spelling; both must compare bare. A port on the
    address is a different destination, though, and must not match a row
    stored without one (Codex, PR #10)."""
    import os
    import subprocess
    from types import SimpleNamespace
    from app import vault

    key = b"6" * 32
    nonce, ct = vault.encrypt_secret(key, "git-endpoint:v6", "ghp_v6")
    row = SimpleNamespace(
        name="v6", method="https_token", username="me", host=stored,
        nonce=nonce, ciphertext=ct,
    )

    def fill(host: str, env: dict) -> str:
        r = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost={host}\n\n", capture_output=True, text=True,
            env={**os.environ, **env, "HOME": str(tmp_path)}, cwd=tmp_path,
        )
        return r.stdout

    assert gitstore.host_of("https://[2001:db8::1]:8443/o/p.git") == "2001:db8::1"
    with gitstore.lease(key, row) as cred:
        assert "password=ghp_v6" in fill("[2001:db8::1]", cred.env)
        assert "password=ghp_v6" in fill("[2001:db8::1]:443", cred.env)
        assert "ghp_v6" not in fill("[2001:db8::1]:8443", cred.env)
        assert "ghp_v6" not in fill("[2001:db8::2]", cred.env)
        assert "ghp_v6" not in fill("[2001", cred.env)


# -- one credential per host --------------------------------------------------


def test_a_second_credential_for_the_same_host_is_refused_by_name(unlocked) -> None:
    """Matching by host is the only selection mechanism there is — the clone
    form is one URL field. A second row for the same host would make every
    clone, pull and push a coin toss between identities, and a push under the
    wrong account is the failure that looks like success. The refusal names the
    row that already holds the host, so the fix is obvious."""
    unlocked.put("/api/settings/git/personal", json={
        "host": "github.com", "method": "https_token", "secret": "a", "username": "me",
    })
    r = unlocked.put("/api/settings/git/work", json={
        "host": "github.com", "method": "https_token", "secret": "b", "username": "corp",
    })

    assert r.status_code == 400
    assert "'personal'" in r.json()["detail"]
    assert [e["name"] for e in unlocked.get("/api/settings/git").json()["endpoints"]] == ["personal"]


def test_rotating_the_same_row_is_not_a_collision(unlocked) -> None:
    for secret in ("tok-1", "tok-2"):
        r = unlocked.put("/api/settings/git/personal", json={
            "host": "github.com", "method": "https_token", "secret": secret, "username": "me",
        })
        assert r.status_code == 200


def test_moving_a_row_onto_an_occupied_host_is_refused_too(unlocked) -> None:
    unlocked.put("/api/settings/git/gh", json={
        "host": "github.com", "method": "https_token", "secret": "a", "username": "x",
    })
    unlocked.put("/api/settings/git/gl", json={
        "host": "gitlab.com", "method": "https_token", "secret": "b", "username": "y",
    })
    r = unlocked.put("/api/settings/git/gl", json={
        "host": "github.com", "method": "https_token", "secret": "b", "username": "y",
    })
    assert r.status_code == 400 and "'gh'" in r.json()["detail"]


def test_two_users_may_each_hold_the_same_host(unlocked, second_user) -> None:
    """The constraint is per user — it exists to stop ambiguity inside one
    user's matching, not to ration hosts across the deployment."""
    unlocked.put("/api/settings/git/github", json={
        "host": "github.com", "method": "https_token", "secret": "a", "username": "me",
    })
    r = second_user.put("/api/settings/git/github", json={
        "host": "github.com", "method": "https_token", "secret": "b", "username": "them",
    })
    assert r.status_code == 200


# -- IPv6 hosts match in either spelling ---------------------------------------


@pytest.fixture()
def store():
    """A git_endpoint table on its own, to write rows the API no longer would."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import GitEndpoint

    engine = create_engine("sqlite://")
    GitEndpoint.__table__.create(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


class _User:
    id = 1


_V6_REMOTES = ["https://[2001:db8::1]/repo", "https://[2001:db8::1]:443/repo"]


@pytest.mark.parametrize("stored", ["[2001:DB8::1]", "2001:db8::1"])
def test_an_ipv6_credential_is_found_for_its_remote(store, stored) -> None:
    """Codex P2 on #10: put kept the brackets, host_of drops them, and the
    exact lookup missed — so a private clone, pull or push went ahead with no
    credential. Stored bare, both spellings match."""
    gitstore.put(store, b"7" * 32, _User(), name="v6", host=stored,
                 method="https_token", secret="ghp_v6", username="me")
    store.flush()
    for remote in _V6_REMOTES:
        row = gitstore.for_remote(store, _User(), remote)
        assert row is not None and row.name == "v6", remote
    assert gitstore.for_remote(store, _User(), "https://[2001:db8::2]/repo") is None
    # Another port on the same address is another service (Codex, PR #10).
    assert gitstore.for_remote(store, _User(), "https://[2001:db8::1]:8443/repo") is None


def test_a_bracketed_row_from_before_the_fix_is_still_found(store) -> None:
    """Rows written before put stored hosts bare still hold the brackets; the
    lookup compares bare on both sides rather than trusting a migration."""
    from app import vault
    from app.models import GitEndpoint

    nonce, ct = vault.encrypt_secret(b"7" * 32, "git-endpoint:legacy", "ghp_old")
    store.add(GitEndpoint(user_id=1, name="legacy", host="[2001:DB8::1]",
                          method="https_token", username="me", nonce=nonce, ciphertext=ct))
    store.flush()
    for remote in _V6_REMOTES:
        row = gitstore.for_remote(store, _User(), remote)
        assert row is not None and row.name == "legacy", remote
    # And it still occupies its host: a second row for the bare spelling would
    # make matching a coin toss between the two.
    with pytest.raises(gitstore.GitStoreError, match="'legacy'"):
        gitstore.put(store, b"7" * 32, _User(), name="other", host="2001:db8::1",
                     method="https_token", secret="ghp_new", username="me")


def _legacy_row(store, name: str, host: str, secret: str) -> None:
    from app import vault
    from app.models import GitEndpoint

    nonce, ct = vault.encrypt_secret(b"7" * 32, f"git-endpoint:{name}", secret)
    store.add(GitEndpoint(user_id=1, name=name, host=host, method="https_token",
                          username="me", nonce=nonce, ciphertext=ct))
    store.flush()


def test_two_legacy_rows_for_one_host_are_refused_not_guessed(store) -> None:
    """Codex P2 on #10: a pre-upgrade database can hold `[2001:db8::1]` and
    `2001:db8::1` as two rows; both match once normalised, and taking the first
    pushed under whichever account sorted first. Now nothing is chosen, the
    refusal names both, and the listing flags the pair until one is removed."""
    _legacy_row(store, "alice", "[2001:db8::1]", "ghp_alice")
    _legacy_row(store, "bob", "2001:db8::1", "ghp_bob")

    with pytest.raises(gitstore.GitStoreError) as exc:
        gitstore.for_remote(store, _User(), "https://[2001:db8::1]/repo")
    msg = str(exc.value)
    assert "two saved credentials match host 2001:db8::1" in msg
    assert "'alice'" in msg and "'bob'" in msg and "Git endpoints" in msg
    assert "ghp_" not in msg

    listing = {e["name"]: e["conflicts_with"] for e in gitstore.list_endpoints(store, _User())}
    assert listing == {"alice": ["bob"], "bob": ["alice"]}

    # Neither row can be rewritten into a third spelling while the other stands.
    with pytest.raises(gitstore.GitStoreError):
        gitstore.put(store, b"7" * 32, _User(), name="alice", host="2001:db8::1",
                     method="https_token", secret="ghp_new", username="me")

    assert gitstore.delete(store, _User(), "alice")
    store.flush()
    row = gitstore.for_remote(store, _User(), "https://[2001:db8::1]/repo")
    assert row is not None and row.name == "bob"
    assert gitstore.list_endpoints(store, _User())[0]["conflicts_with"] == []


def test_a_credential_collision_reaches_git_operations_as_a_project_error(store) -> None:
    """Clone, pull and push turn ProjectError into a 400; a collision must take
    that route rather than surface as a 500."""
    from app.main import _git_credentials
    from app.projects import ProjectError

    _legacy_row(store, "alice", "[2001:db8::1]", "ghp_alice")
    _legacy_row(store, "bob", "2001:db8::1", "ghp_bob")
    with pytest.raises(ProjectError, match="two saved credentials"):
        with _git_credentials(store, b"7" * 32, _User(), "https://[2001:db8::1]/repo"):
            pass


def test_a_host_with_spaces_is_refused_without_echoing_it(store) -> None:
    """A token pasted where the host goes, missing the @ that would have
    caught it, used to come back quoted in the refusal."""
    with pytest.raises(gitstore.GitStoreError) as exc:
        gitstore.put(store, b"7" * 32, _User(), name="gh", host="ghp_tokensecret github.com",
                     method="https_token", secret="x", username="me")
    assert "ghp_tokensecret" not in str(exc.value)


# -- a credential is scoped to host and port -----------------------------------


def _fill(tmp_path, env: dict, host: str, protocol: str = "https") -> str:
    """What a real `git credential fill` gets back through the leased helper."""
    import os
    import subprocess

    r = subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol={protocol}\nhost={host}\n\n", capture_output=True, text=True,
        env={**os.environ, **env, "HOME": str(tmp_path)}, cwd=tmp_path,
    )
    return r.stdout


def _saved(store, host: str, secret: str = "ghp_corp", name: str = "corp"):
    gitstore.put(store, b"7" * 32, _User(), name=name, host=host,
                 method="https_token", secret=secret, username="me")
    store.flush()


def test_a_token_for_a_host_is_not_released_to_another_port_on_it(store, tmp_path) -> None:
    """Codex P1 on #10: git supplies `host=git.corp.example:8443`, and the
    helper cut the port off before comparing — so a token saved for
    `git.corp.example` went to whatever service answered on 8443. Neither the
    lookup nor the helper may treat every port on a hostname as one place."""
    _saved(store, "git.corp.example")

    assert gitstore.for_remote(store, _User(), "https://git.corp.example:8443/x") is None
    for remote in ("https://git.corp.example/x", "https://git.corp.example:443/x",
                   "https://GIT.corp.example/x"):
        row = gitstore.for_remote(store, _User(), remote)
        assert row is not None and row.name == "corp", remote

    with gitstore.lease(b"7" * 32, gitstore.for_remote(
            store, _User(), "https://git.corp.example/x")) as cred:
        assert "ghp_corp" not in _fill(tmp_path, cred.env, "git.corp.example:8443")
        assert "password=ghp_corp" in _fill(tmp_path, cred.env, "git.corp.example")
        assert "password=ghp_corp" in _fill(tmp_path, cred.env, "git.corp.example:443")
        # 443 is only the default over https; over http it is a named port.
        assert "ghp_corp" not in _fill(tmp_path, cred.env, "git.corp.example:443", "http")


def test_a_token_saved_with_a_port_is_released_only_for_that_port(store, tmp_path) -> None:
    _saved(store, "git.corp.example:8443")

    row = gitstore.for_remote(store, _User(), "https://git.corp.example:8443/x")
    assert row is not None and row.host == "git.corp.example:8443"
    assert gitstore.for_remote(store, _User(), "https://git.corp.example/x") is None
    assert gitstore.for_remote(store, _User(), "https://git.corp.example:9443/x") is None

    with gitstore.lease(b"7" * 32, row) as cred:
        assert "password=ghp_corp" in _fill(tmp_path, cred.env, "git.corp.example:8443")
        assert "ghp_corp" not in _fill(tmp_path, cred.env, "git.corp.example")
        assert "ghp_corp" not in _fill(tmp_path, cred.env, "git.corp.example:9443")


def test_an_ipv6_token_saved_with_a_port_is_released_only_for_that_port(store, tmp_path) -> None:
    _saved(store, "[2001:DB8::1]:8443")

    row = gitstore.for_remote(store, _User(), "https://[2001:db8::1]:8443/x")
    assert row is not None and row.host == "[2001:db8::1]:8443"
    assert gitstore.for_remote(store, _User(), "https://[2001:db8::1]/x") is None

    with gitstore.lease(b"7" * 32, row) as cred:
        assert "password=ghp_corp" in _fill(tmp_path, cred.env, "[2001:db8::1]:8443")
        assert "ghp_corp" not in _fill(tmp_path, cred.env, "[2001:db8::1]")
        assert "ghp_corp" not in _fill(tmp_path, cred.env, "[2001:db8::2]:8443")


def test_the_default_port_is_the_same_credential_and_another_port_is_not(store) -> None:
    """`host` and `host:443` are one destination over HTTPS, so they collide;
    `host:8443` is another, so a second identity may hold it."""
    _saved(store, "git.corp.example:443", name="corp")
    assert gitstore.list_endpoints(store, _User())[0]["host"] == "git.corp.example"
    with pytest.raises(gitstore.GitStoreError, match="'corp'"):
        _saved(store, "git.corp.example", name="other")

    _saved(store, "git.corp.example:8443", secret="ghp_alt", name="alt")
    assert gitstore.for_remote(store, _User(), "https://git.corp.example:8443/x").name == "alt"
    assert gitstore.for_remote(store, _User(), "https://git.corp.example/x").name == "corp"
    assert all(e["conflicts_with"] == [] for e in gitstore.list_endpoints(store, _User()))


def test_a_legacy_row_with_the_default_port_spelled_out_collides_with_the_bare_one(store) -> None:
    """A row from before normalisation can hold `host:443`; that is the same
    destination as `host`, so the pair is a collision, not two scopes."""
    _legacy_row(store, "alice", "git.corp.example:443", "ghp_alice")
    _legacy_row(store, "bob", "git.corp.example", "ghp_bob")
    with pytest.raises(gitstore.GitStoreError, match="two saved credentials"):
        gitstore.for_remote(store, _User(), "https://git.corp.example/x")
    listing = {e["name"]: e["conflicts_with"] for e in gitstore.list_endpoints(store, _User())}
    assert listing == {"alice": ["bob"], "bob": ["alice"]}


def test_an_ssh_key_is_scoped_to_its_port_too(store) -> None:
    """The same rule over SSH: a key saved for `git.corp.example` is for port
    22, and an `ssh://...:2222` remote is another service."""
    gitstore.put(store, b"7" * 32, _User(), name="key", host="git.corp.example",
                 method="ssh_key", secret="-----BEGIN KEY-----")
    store.flush()
    for remote in ("ssh://git@git.corp.example/x.git", "ssh://git@git.corp.example:22/x.git",
                   "git@git.corp.example:x.git"):
        assert gitstore.for_remote(store, _User(), remote) is not None, remote
    assert gitstore.for_remote(store, _User(), "ssh://git@git.corp.example:2222/x.git") is None


@pytest.mark.parametrize("host", ["git.corp.example:http", "git.corp.example:0",
                                  "git.corp.example:70000", "[2001:db8::1", "[2001:db8::1]x"])
def test_a_host_with_an_unreadable_port_is_refused(store, host) -> None:
    with pytest.raises(gitstore.GitStoreError, match=":port"):
        _saved(store, host)
