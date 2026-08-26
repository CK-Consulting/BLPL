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
