"""Sign in with GitLab, GitHub, or Google.

The app's front door was a passphrase. This adds a second one: prove who you are
at an identity provider you already use, and the server opens the vault with the
key it holds (app/serverkey.py). What this module does is establish *identity*
and nothing else — it never sees the DEK and never opens a session itself. That
separation is deliberate, because the two questions have different answers:
"who is this" is what an IdP can tell you, and "may they have the keys" is ours.

Two protocols, not one
----------------------

GitLab and Google are OpenID Connect providers: they publish a discovery
document at ``/.well-known/openid-configuration``, and everything — authorize
endpoint, token endpoint, userinfo — is read from it, so a self-hosted GitLab
needs a URL and no code.

GitHub is *not* an OIDC provider. It publishes no discovery document (that URL
is a 404), issues no id_token, and has no JWKS. It is plain OAuth2: swap the code
for an access token, then ask ``api.github.com`` who the token belongs to. So it
gets its own small branch rather than being bent into the OIDC shape.

We deliberately do not parse or verify id_tokens. Identity comes from calling
userinfo with the access token we just received over the back channel. That is
one extra round trip, and it removes a JWT library, a JWKS cache, and a class of
signature-verification bugs from a security-critical path. The token came
straight from the provider's token endpoint over TLS with our client secret, so
there is no third party to have forged it in transit.

Nothing here adds a dependency: urllib and hmac are stdlib, which keeps
requirements.txt the five deliberate pins it already is.

Who is allowed in
-----------------

An IdP tells you the login is genuine, not that it is *yours*. "Sign in with
Google" with no further check means anyone on earth with a Google account opens
your vault. So the allowlist fails closed: with neither BLPL_OAUTH_ALLOWED_EMAILS
nor BLPL_OAUTH_ALLOWED_DOMAINS set, SSO reports itself unconfigured and every
callback is refused. Unverified provider emails are refused too — an unverified
address is a claim, not an identity, and on a domain allowlist it is forgeable.

The signed state carrier follows the shape used in KiCAD-Prism
(Apache-2.0, krishna-swaroop/KiCAD-Prism): an HMAC over a compact JSON payload
carrying its own expiry. Written fresh here against this app's config, but the
design is theirs and worth the credit.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

_HTTP_TIMEOUT = 15
_STATE_TTL_SECONDS = 600
_DISCOVERY_TTL_SECONDS = 3600


class OAuthError(RuntimeError):
    """The login could not be completed. The message is shown to the user, so it
    says what to do rather than what broke internally."""


class NotAllowed(OAuthError):
    """The identity was genuine but is not on this install's allowlist. Kept
    distinct so the callback can log a refusal without it looking like an
    outage."""


@dataclass(frozen=True)
class Provider:
    """One configured way in. ``kind`` selects the protocol branch: "oidc" reads
    a discovery document, "github" uses the hardcoded GitHub endpoints."""

    id: str
    label: str
    kind: str
    client_id: str
    client_secret: str
    issuer: str = ""
    scopes: str = "openid email profile"


@dataclass(frozen=True)
class Identity:
    """Who the provider says this is. ``email`` is the only field the allowlist
    consults; the rest is for display."""

    email: str
    name: str
    subject: str
    provider: str


# Providers we know how to drive, and the environment variables that turn each
# on. A provider with no client id configured simply does not appear.
#
# gitlab-self exists as a separate entry rather than as an override of gitlab so
# a deploy can offer both gitlab.com and a company GitLab at once, which is the
# normal state of affairs for anyone who has a company GitLab.
_PROVIDER_SPECS = (
    ("gitlab", "GitLab", "oidc", "https://gitlab.com", "openid email profile"),
    ("gitlab-self", "GitLab (self-hosted)", "oidc", "", "openid email profile"),
    ("google", "Google", "oidc", "https://accounts.google.com", "openid email profile"),
    ("github", "GitHub", "github", "", "read:user user:email"),
)

_discovery_cache: dict[str, tuple[float, dict]] = {}


def _env(provider_id: str, suffix: str) -> str:
    key = f"BLPL_OAUTH_{provider_id.upper().replace('-', '_')}_{suffix}"
    return os.environ.get(key, "").strip()


def providers() -> dict[str, Provider]:
    """Every provider with credentials configured, keyed by id.

    Empty when nothing is set up, which is the normal state for a passphrase-only
    install — SSO is opt-in and its absence is not an error anywhere.
    """
    found: dict[str, Provider] = {}
    for pid, label, kind, default_issuer, scopes in _PROVIDER_SPECS:
        client_id = _env(pid, "CLIENT_ID")
        client_secret = _env(pid, "CLIENT_SECRET")
        if not client_id or not client_secret:
            continue
        issuer = _env(pid, "ISSUER") or default_issuer
        if kind == "oidc" and not issuer:
            # gitlab-self with no issuer is a misconfiguration, not a provider.
            # Skipping it quietly beats redirecting the user to "https:///...".
            continue
        found[pid] = Provider(
            id=pid,
            label=label,
            kind=kind,
            client_id=client_id,
            client_secret=client_secret,
            issuer=issuer.rstrip("/"),
            scopes=scopes,
        )
    return found


def allowlist() -> tuple[set[str], set[str]]:
    """(emails, domains) permitted to sign in, both lowercased."""
    emails = {
        e.strip().lower()
        for e in os.environ.get("BLPL_OAUTH_ALLOWED_EMAILS", "").split(",")
        if e.strip()
    }
    domains = {
        d.strip().lower().lstrip("@")
        for d in os.environ.get("BLPL_OAUTH_ALLOWED_DOMAINS", "").split(",")
        if d.strip()
    }
    return emails, domains


def sso_configured() -> bool:
    """Whether SSO is offerable at all: at least one provider *and* an allowlist.

    Both halves are required. A provider with no allowlist would let any account
    at that provider into the vault, so it is treated as not configured rather
    than as configured-and-open — the safe reading of an incomplete setup.
    """
    emails, domains = allowlist()
    return bool(providers()) and bool(emails or domains)


def check_allowed(identity: Identity) -> None:
    """Raise NotAllowed unless this identity is on the allowlist."""
    emails, domains = allowlist()
    if not emails and not domains:
        raise NotAllowed(
            "no allowlist is configured, so SSO is refused. Set "
            "BLPL_OAUTH_ALLOWED_EMAILS or BLPL_OAUTH_ALLOWED_DOMAINS."
        )
    email = identity.email.strip().lower()
    if email in emails:
        return
    if domains and email.rsplit("@", 1)[-1] in domains:
        return
    raise NotAllowed(f"{identity.email} is not permitted to sign in to this server")


# ---------------------------------------------------------------------------
# Signed state
# ---------------------------------------------------------------------------


def sign_state(secret: bytes, provider_id: str, nonce: str) -> str:
    """A tamper-evident, self-expiring state parameter.

    Signed rather than stored so it survives a restart mid-login and needs no
    server-side table to garbage-collect. The nonce is echoed in a cookie and
    compared on return, which is what actually binds the callback to the browser
    that started it — a signature alone would let an attacker replay their own
    valid state into your session (login CSRF).
    """
    payload = {"p": provider_id, "n": nonce, "exp": int(time.time()) + _STATE_TTL_SECONDS}
    raw = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{raw}.{_b64(hmac.new(secret, raw.encode(), hashlib.sha256).digest())}"


def verify_state(secret: bytes, state: str, cookie_nonce: str) -> str:
    """Return the provider id carried by a valid state, or raise OAuthError."""
    parts = state.split(".")
    if len(parts) != 2:
        raise OAuthError("malformed sign-in state")
    raw, signature = parts
    expected = _b64(hmac.new(secret, raw.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise OAuthError("sign-in state failed its signature check")
    try:
        payload = json.loads(_unb64(raw))
    except (ValueError, json.JSONDecodeError) as exc:
        raise OAuthError("unreadable sign-in state") from exc
    if int(payload.get("exp", 0)) <= int(time.time()):
        raise OAuthError("this sign-in took too long — start again")
    if not cookie_nonce or not hmac.compare_digest(str(payload.get("n", "")), cookie_nonce):
        raise OAuthError("sign-in did not start in this browser")
    return str(payload.get("p", ""))


def new_nonce() -> str:
    return secrets.token_urlsafe(16)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


def authorize_url(provider: Provider, redirect_uri: str, state: str) -> str:
    """Where to send the browser to start the login."""
    params = {
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": provider.scopes,
        "state": state,
    }
    return f"{_authorization_endpoint(provider)}?{urllib.parse.urlencode(params)}"


def exchange_code(provider: Provider, code: str, redirect_uri: str) -> str:
    """Swap the authorization code for an access token.

    Sends the client secret in the POST body (``client_secret_post``), which all
    four providers accept, rather than branching on client_secret_basic as well.
    """
    data = urllib.parse.urlencode(
        {
            "client_id": provider.client_id,
            "client_secret": provider.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode()
    payload = _post(_token_endpoint(provider), data)
    token = str(payload.get("access_token") or "")
    if not token:
        # Providers report a failed exchange in the body with a 200, so an empty
        # token is the error path, not an impossibility.
        raise OAuthError(f"{provider.label} did not issue a token: {payload.get('error') or payload}")
    return token


def fetch_identity(provider: Provider, access_token: str) -> Identity:
    """Ask the provider who this token belongs to."""
    if provider.kind == "github":
        return _github_identity(provider, access_token)
    return _oidc_identity(provider, access_token)


def _oidc_identity(provider: Provider, access_token: str) -> Identity:
    profile = _get(_userinfo_endpoint(provider), access_token)
    email = str(profile.get("email") or "").strip()
    if not email:
        raise OAuthError(f"{provider.label} returned no email address for this account")
    # email_verified absent is treated as unverified. Google and GitLab both send
    # it; a provider that does not is one we cannot vouch for, and a domain
    # allowlist is only as good as the verification behind the address.
    if not profile.get("email_verified"):
        raise OAuthError(f"{provider.label} reports {email} as unverified")
    return Identity(
        email=email,
        name=str(profile.get("name") or email.split("@")[0]),
        subject=str(profile.get("sub") or email),
        provider=provider.id,
    )


def _github_identity(provider: Provider, access_token: str) -> Identity:
    """GitHub's plain-OAuth2 path.

    Two calls, because /user's ``email`` is null whenever the user keeps their
    address private — which is the default. The address that matters is the
    primary verified one from /user/emails, and if there isn't one we refuse
    rather than fall back to the login name.
    """
    user = _get("https://api.github.com/user", access_token)
    emails = _get("https://api.github.com/user/emails", access_token)
    primary = ""
    if isinstance(emails, list):
        for entry in emails:
            if entry.get("primary") and entry.get("verified"):
                primary = str(entry.get("email") or "")
                break
    if not primary:
        raise OAuthError("GitHub has no primary verified email on this account")
    return Identity(
        email=primary,
        name=str(user.get("name") or user.get("login") or primary),
        subject=str(user.get("id") or primary),
        provider=provider.id,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_GITHUB_ENDPOINTS = {
    "authorization_endpoint": "https://github.com/login/oauth/authorize",
    "token_endpoint": "https://github.com/login/oauth/access_token",
}


def _authorization_endpoint(provider: Provider) -> str:
    if provider.kind == "github":
        return _GITHUB_ENDPOINTS["authorization_endpoint"]
    return _require_endpoint(provider, "authorization_endpoint")


def _token_endpoint(provider: Provider) -> str:
    if provider.kind == "github":
        return _GITHUB_ENDPOINTS["token_endpoint"]
    return _require_endpoint(provider, "token_endpoint")


def _userinfo_endpoint(provider: Provider) -> str:
    return _require_endpoint(provider, "userinfo_endpoint")


def _require_endpoint(provider: Provider, name: str) -> str:
    value = str(discovery(provider).get(name) or "")
    if not value:
        raise OAuthError(f"{provider.label} does not publish a {name}")
    return value


def discovery(provider: Provider) -> dict:
    """The provider's OIDC discovery document, cached for an hour.

    Cached because it is fetched on every leg of every login and changes about
    never; an hour is short enough that a provider rotating an endpoint is
    picked up without a restart.
    """
    url = f"{provider.issuer}/.well-known/openid-configuration"
    hit = _discovery_cache.get(url)
    if hit and hit[0] > time.time():
        return hit[1]
    payload = _get(url, None)
    _discovery_cache[url] = (time.time() + _DISCOVERY_TTL_SECONDS, payload)
    return payload


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get(url: str, access_token: str | None) -> dict | list:
    headers = {"Accept": "application/json", "User-Agent": "blpl"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return _request(urllib.request.Request(url, headers=headers), url)


def _post(url: str, data: bytes) -> dict:
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            # Without this GitHub answers form-encoded, and json.loads chokes on
            # a body that looks nothing like an error.
            "Accept": "application/json",
            "User-Agent": "blpl",
        },
    )
    payload = _request(req, url)
    if not isinstance(payload, dict):
        raise OAuthError(f"unexpected response from {url}")
    return payload


def _request(req: urllib.request.Request, url: str) -> dict | list:
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # The provider's own body usually says exactly what is wrong
        # ("redirect_uri_mismatch"), and hiding it behind the status code turns a
        # five-second fix into an afternoon.
        detail = exc.read()[:300].decode("utf-8", "replace").strip()
        raise OAuthError(f"{url} returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OAuthError(f"could not reach {url}: {exc}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise OAuthError(f"unreadable response from {url}") from exc
