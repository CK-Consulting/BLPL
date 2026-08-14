"""Signing in with GitLab / GitHub / Google, and the second DEK wrapping it needs.

Two things are being pinned here, and they fail in different directions:

* The **crypto**: an SSO login must open the *same* DEK the passphrase opens, or
  every secret already stored becomes unreadable to anyone who signs in.
* The **gate**: an identity provider says a login is genuine, not that it is
  yours. Every way the allowlist could be bypassed is a way into the vault, so
  the fail-closed cases matter more than the happy path.

The provider is stubbed at the HTTP boundary (app.oauth._get / _post). Nothing
here talks to gitlab.com — the flow's shape is ours to get right, and a test that
needs the internet is a test that gets skipped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app import serverkey, vault  # noqa: E402

_OAUTH_VARS = (
    "BLPL_OAUTH_GITLAB_CLIENT_ID", "BLPL_OAUTH_GITLAB_CLIENT_SECRET", "BLPL_OAUTH_GITLAB_ISSUER",
    "BLPL_OAUTH_GITLAB_SELF_CLIENT_ID", "BLPL_OAUTH_GITLAB_SELF_CLIENT_SECRET",
    "BLPL_OAUTH_GITLAB_SELF_ISSUER",
    "BLPL_OAUTH_GOOGLE_CLIENT_ID", "BLPL_OAUTH_GOOGLE_CLIENT_SECRET",
    "BLPL_OAUTH_GITHUB_CLIENT_ID", "BLPL_OAUTH_GITHUB_CLIENT_SECRET",
    "BLPL_OAUTH_ALLOWED_EMAILS", "BLPL_OAUTH_ALLOWED_DOMAINS",
    "BLPL_SERVER_KEY", "BLPL_PUBLIC_URL",
)


@pytest.fixture
def clean_env(monkeypatch):
    """No SSO configuration leaking in from the developer's own shell."""
    for var in _OAUTH_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def gitlab(clean_env, monkeypatch):
    monkeypatch.setenv("BLPL_OAUTH_GITLAB_CLIENT_ID", "cid")
    monkeypatch.setenv("BLPL_OAUTH_GITLAB_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("BLPL_OAUTH_ALLOWED_EMAILS", "shaun@example.com")
    from app import oauth

    oauth._discovery_cache.clear()
    return oauth


# ---------------------------------------------------------------------------
# The second wrapping
# ---------------------------------------------------------------------------


def test_both_wrappings_open_the_same_data_key():
    """The whole design rests on this. If SSO produced a different DEK, every
    secret stored under the passphrase would decrypt to garbage."""
    params, wrapped = vault.init_vault("correct-horse-staple")
    dek = vault.unlock("correct-horse-staple", params, wrapped)

    key = serverkey.generate()
    by_server = vault.unwrap_dek_with_key(key, vault.wrap_dek_with_key(key, dek))

    assert by_server == dek


def test_a_wrong_server_key_is_refused_and_named_as_such():
    wrapped = vault.wrap_dek_with_key(serverkey.generate(), b"k" * 32)
    with pytest.raises(vault.WrongServerKey):
        vault.unwrap_dek_with_key(serverkey.generate(), wrapped)


def test_the_two_wrappings_are_not_interchangeable():
    """Different AAD, so a passphrase-wrapped blob fed to the server path fails
    loudly rather than being decrypted by a key that happens to fit."""
    key = serverkey.generate()
    params, passphrase_wrapped = vault.init_vault("correct-horse-staple")
    dek = vault.unlock("correct-horse-staple", params, passphrase_wrapped)
    server_wrapped = vault.wrap_dek_with_key(key, dek)

    with pytest.raises(vault.VaultError):
        vault.unwrap_dek_with_key(key, passphrase_wrapped)
    # And the reverse: the server blob is not a passphrase blob.
    with pytest.raises(vault.WrongPassphrase):
        vault.unlock("correct-horse-staple", params, server_wrapped)


def test_a_short_server_key_is_rejected_rather_than_silently_weaker():
    with pytest.raises(vault.VaultError):
        vault.wrap_dek_with_key(b"too-short", b"k" * 32)


def test_a_group_readable_key_file_is_refused(tmp_path, clean_env):
    serverkey.load_or_create(tmp_path)
    serverkey.key_path(tmp_path).chmod(0o644)
    with pytest.raises(serverkey.ServerKeyError):
        serverkey.load(tmp_path)


def test_reading_never_creates_a_key(tmp_path, clean_env):
    """"Is SSO on?" must not be the question that turns it on."""
    assert serverkey.load(tmp_path) is None
    assert not serverkey.key_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# The signed state
# ---------------------------------------------------------------------------


def test_state_round_trips_with_its_nonce(gitlab):
    key = serverkey.generate()
    nonce = gitlab.new_nonce()
    state = gitlab.sign_state(key, "gitlab", nonce)
    assert gitlab.verify_state(key, state, nonce) == "gitlab"


def test_a_tampered_state_is_refused(gitlab):
    key = serverkey.generate()
    nonce = gitlab.new_nonce()
    payload, signature = gitlab.sign_state(key, "gitlab", nonce).split(".")
    forged = gitlab.sign_state(key, "github", nonce).split(".")[0]
    with pytest.raises(gitlab.OAuthError):
        gitlab.verify_state(key, f"{forged}.{signature}", nonce)


def test_state_signed_with_another_key_is_refused(gitlab):
    nonce = gitlab.new_nonce()
    state = gitlab.sign_state(serverkey.generate(), "gitlab", nonce)
    with pytest.raises(gitlab.OAuthError):
        gitlab.verify_state(serverkey.generate(), state, nonce)


def test_a_state_without_its_browser_cookie_is_refused(gitlab):
    """Login CSRF: a valid state the attacker minted for their own account must
    not complete in someone else's browser. The signature alone cannot tell
    those apart — the nonce echo is what does."""
    key = serverkey.generate()
    state = gitlab.sign_state(key, "gitlab", gitlab.new_nonce())
    with pytest.raises(gitlab.OAuthError):
        gitlab.verify_state(key, state, "")
    with pytest.raises(gitlab.OAuthError):
        gitlab.verify_state(key, state, "some-other-nonce")


def test_an_expired_state_is_refused(gitlab, monkeypatch):
    key = serverkey.generate()
    nonce = gitlab.new_nonce()
    state = gitlab.sign_state(key, "gitlab", nonce)
    monkeypatch.setattr(gitlab.time, "time", lambda: 10**10)
    with pytest.raises(gitlab.OAuthError):
        gitlab.verify_state(key, state, nonce)


# ---------------------------------------------------------------------------
# Who gets in
# ---------------------------------------------------------------------------


def test_providers_with_no_allowlist_are_not_offered(clean_env, monkeypatch):
    """A provider and no allowlist would let anyone with a GitLab account open
    the vault. Treated as unconfigured, not as configured-and-open."""
    monkeypatch.setenv("BLPL_OAUTH_GITLAB_CLIENT_ID", "cid")
    monkeypatch.setenv("BLPL_OAUTH_GITLAB_CLIENT_SECRET", "csecret")
    from app import oauth

    assert "gitlab" in oauth.providers()
    assert oauth.sso_configured() is False


def test_check_allowed_refuses_when_nothing_is_configured(clean_env):
    from app import oauth

    who = oauth.Identity(email="anyone@example.com", name="A", subject="1", provider="gitlab")
    with pytest.raises(oauth.NotAllowed):
        oauth.check_allowed(who)


def test_an_allowed_domain_admits_and_others_do_not(clean_env, monkeypatch):
    monkeypatch.setenv("BLPL_OAUTH_ALLOWED_DOMAINS", "example.com")
    from app import oauth

    oauth.check_allowed(oauth.Identity("shaun@example.com", "S", "1", "gitlab"))
    with pytest.raises(oauth.NotAllowed):
        oauth.check_allowed(oauth.Identity("someone@evil.com", "E", "2", "gitlab"))


def test_a_lookalike_domain_does_not_match(clean_env, monkeypatch):
    """Suffix matching would admit notexample.com and example.com.evil.net."""
    monkeypatch.setenv("BLPL_OAUTH_ALLOWED_DOMAINS", "example.com")
    from app import oauth

    for address in ("shaun@notexample.com", "shaun@example.com.evil.net", "shaun@evil.net"):
        with pytest.raises(oauth.NotAllowed):
            oauth.check_allowed(oauth.Identity(address, "S", "1", "gitlab"))


def test_an_unverified_provider_email_is_refused(gitlab, monkeypatch):
    """An unverified address is a claim, not an identity — and on a domain
    allowlist it is trivially forgeable."""
    monkeypatch.setattr(
        gitlab, "_get",
        lambda url, token: {"userinfo_endpoint": "https://gitlab.com/oauth/userinfo"}
        if "openid-configuration" in url
        else {"email": "shaun@example.com", "email_verified": False, "sub": "1"},
    )
    with pytest.raises(gitlab.OAuthError, match="unverified"):
        gitlab.fetch_identity(gitlab.providers()["gitlab"], "tok")


def test_github_uses_the_primary_verified_address_not_the_profile_one(clean_env, monkeypatch):
    """/user's email is null whenever the address is private, which is the
    default — so the profile call alone would strand most GitHub users."""
    monkeypatch.setenv("BLPL_OAUTH_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("BLPL_OAUTH_GITHUB_CLIENT_SECRET", "csecret")
    from app import oauth

    def fake_get(url, token):
        if url.endswith("/user"):
            return {"login": "shaun", "name": "Shaun", "id": 7, "email": None}
        return [
            {"email": "old@example.com", "primary": False, "verified": True},
            {"email": "shaun@example.com", "primary": True, "verified": True},
        ]

    monkeypatch.setattr(oauth, "_get", fake_get)
    who = oauth.fetch_identity(oauth.providers()["github"], "tok")
    assert who.email == "shaun@example.com"


def test_github_without_a_verified_address_is_refused(clean_env, monkeypatch):
    monkeypatch.setenv("BLPL_OAUTH_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("BLPL_OAUTH_GITHUB_CLIENT_SECRET", "csecret")
    from app import oauth

    monkeypatch.setattr(
        oauth, "_get",
        lambda url, token: {"login": "shaun", "id": 7}
        if url.endswith("/user")
        else [{"email": "shaun@example.com", "primary": True, "verified": False}],
    )
    with pytest.raises(oauth.OAuthError):
        oauth.fetch_identity(oauth.providers()["github"], "tok")


# ---------------------------------------------------------------------------
# The whole flow, through the API
# ---------------------------------------------------------------------------


@pytest.fixture
def sso_app(client, tmp_path, clean_env, monkeypatch):
    """A reloaded app with GitLab configured, an allowlist, and the vault
    initialized — i.e. the state an operator is in after their first unlock."""
    monkeypatch.setenv("BLPL_OAUTH_GITLAB_CLIENT_ID", "cid")
    monkeypatch.setenv("BLPL_OAUTH_GITLAB_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("BLPL_OAUTH_ALLOWED_EMAILS", "shaun@example.com")
    monkeypatch.setenv("BLPL_PUBLIC_URL", "https://blpl.example")
    import app.main as main

    main.oauth._discovery_cache.clear()
    # The passphrase unlock is what enrols the server-key wrapping.
    client.post("/api/auth/initialize", json={"passphrase": "correct-horse-staple"})
    monkeypatch.setattr(main.oauth, "exchange_code", lambda p, c, r: "access-token")
    monkeypatch.setattr(
        main.oauth, "fetch_identity",
        lambda p, t: main.oauth.Identity("shaun@example.com", "Shaun", "1", p.id),
    )
    return main, client


def test_the_first_passphrase_unlock_enrols_sso(sso_app):
    main, client = sso_app
    assert main.identity.sso_enabled() is True
    status = client.get("/api/auth/status").json()
    assert status["sso_ready"] is True
    assert {p["id"] for p in status["sso_providers"]} == {"gitlab"}


def test_sso_is_not_offered_before_the_vault_is_enrolled(client, clean_env, monkeypatch):
    """Providers configured but nobody has unlocked yet: there is no wrapping to
    unwrap, so the button would be a dead end."""
    monkeypatch.setenv("BLPL_OAUTH_GITLAB_CLIENT_ID", "cid")
    monkeypatch.setenv("BLPL_OAUTH_GITLAB_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("BLPL_OAUTH_ALLOWED_EMAILS", "shaun@example.com")

    status = client.get("/api/auth/status").json()
    assert status["sso_ready"] is False


def test_a_full_login_opens_a_session(sso_app):
    main, client = sso_app
    client.post("/api/auth/lock")
    assert client.get("/api/auth/status").json()["unlocked"] is False

    started = client.get("/api/auth/oauth/gitlab/start", follow_redirects=False)
    assert started.status_code == 200
    nonce = client.cookies.get("blpl_oauth_nonce")
    state = main.oauth.sign_state(main.serverkey.load_or_create(main._DATA), "gitlab", nonce)

    done = client.get(f"/api/auth/oauth/gitlab/callback?code=abc&state={state}")
    assert done.status_code == 200
    assert client.get("/api/auth/status").json()["unlocked"] is True


def test_a_login_from_outside_the_allowlist_opens_nothing(sso_app, monkeypatch):
    main, client = sso_app
    client.post("/api/auth/lock")
    monkeypatch.setattr(
        main.oauth, "fetch_identity",
        lambda p, t: main.oauth.Identity("intruder@evil.com", "E", "2", p.id),
    )

    client.get("/api/auth/oauth/gitlab/start")
    nonce = client.cookies.get("blpl_oauth_nonce")
    state = main.oauth.sign_state(main.serverkey.load_or_create(main._DATA), "gitlab", nonce)

    done = client.get(f"/api/auth/oauth/gitlab/callback?code=abc&state={state}")
    assert "not permitted" in done.text
    assert client.get("/api/auth/status").json()["unlocked"] is False


def test_a_callback_without_the_browser_nonce_opens_nothing(sso_app):
    main, client = sso_app
    client.post("/api/auth/lock")
    # A state the attacker minted for themselves, replayed into a browser that
    # never started this login.
    state = main.oauth.sign_state(main.serverkey.load_or_create(main._DATA), "gitlab", "attacker-nonce")
    client.cookies.delete("blpl_oauth_nonce")

    done = client.get(f"/api/auth/oauth/gitlab/callback?code=abc&state={state}")
    assert "did not start in this browser" in done.text
    assert client.get("/api/auth/status").json()["unlocked"] is False


def test_a_state_for_another_provider_is_refused(sso_app, monkeypatch):
    main, client = sso_app
    monkeypatch.setenv("BLPL_OAUTH_GITHUB_CLIENT_ID", "cid")
    monkeypatch.setenv("BLPL_OAUTH_GITHUB_CLIENT_SECRET", "csecret")
    client.post("/api/auth/lock")

    client.get("/api/auth/oauth/gitlab/start")
    nonce = client.cookies.get("blpl_oauth_nonce")
    state = main.oauth.sign_state(main.serverkey.load_or_create(main._DATA), "gitlab", nonce)

    done = client.get(f"/api/auth/oauth/github/callback?code=abc&state={state}")
    assert "different provider" in done.text
    assert client.get("/api/auth/status").json()["unlocked"] is False


def test_disabling_sso_leaves_the_passphrase_working(sso_app):
    main, client = sso_app
    assert client.post("/api/auth/oauth/disable").json() == {"disabled": True}
    assert main.identity.sso_enabled() is False

    client.post("/api/auth/lock")
    # The secrets and the passphrase are untouched by the removal.
    assert client.post("/api/auth/unlock", json={"passphrase": "correct-horse-staple"}).status_code == 200


def test_a_secret_stored_under_the_passphrase_is_readable_after_an_sso_login(sso_app):
    """The point of sharing one DEK. If SSO opened a different key, everything
    stored before the first SSO login would be lost."""
    main, client = sso_app
    token = client.cookies.get("blpl_session")
    main.identity.set_secret(token, "anthropic", "sk-stored-under-passphrase")

    client.post("/api/auth/lock")
    client.get("/api/auth/oauth/gitlab/start")
    nonce = client.cookies.get("blpl_oauth_nonce")
    state = main.oauth.sign_state(main.serverkey.load_or_create(main._DATA), "gitlab", nonce)
    client.get(f"/api/auth/oauth/gitlab/callback?code=abc&state={state}")

    sso_token = client.cookies.get("blpl_session")
    assert main.identity.get_secret(sso_token, "anthropic") == "sk-stored-under-passphrase"


def test_self_hosted_gitlab_needs_an_issuer(clean_env, monkeypatch):
    """Without one the authorize URL would be 'https:///oauth/authorize'."""
    monkeypatch.setenv("BLPL_OAUTH_GITLAB_SELF_CLIENT_ID", "cid")
    monkeypatch.setenv("BLPL_OAUTH_GITLAB_SELF_CLIENT_SECRET", "csecret")
    from app import oauth

    assert "gitlab-self" not in oauth.providers()
    monkeypatch.setenv("BLPL_OAUTH_GITLAB_SELF_ISSUER", "https://git.internal/")
    assert oauth.providers()["gitlab-self"].issuer == "https://git.internal"
