"""Verifying a Clerk session on a Python backend.

Clerk's own SDKs are JavaScript, and the frontend gets one. The backend does not,
so it does what any resource server does with a JWT: fetch the issuer's public
keys and check the signature itself. That is all this module is.

The session token arrives either as ``Authorization: Bearer <jwt>`` (what the
frontend sends after calling Clerk's getToken) or as the ``__session`` cookie
Clerk sets on the same origin. Both are checked; neither is trusted until
verified.

What is actually enforced
-------------------------

Signature against Clerk's published JWKS, ``exp`` and ``nbf``, and the issuer.
The issuer check is the one that is easy to skip and must not be: without it, a
correctly-signed token from *someone else's* Clerk instance verifies happily, and
anyone can create a Clerk instance. The JWKS URL alone does not pin identity,
because the URL is derived from configuration that the token itself could
otherwise influence.

Deliberately not enforced: ``azp``. Clerk populates it from the browser origin,
which varies across the dev server, the container, and any proxy in front — a
check that fails differently in each environment teaches people to disable it.
The issuer plus the signature is what establishes the token is ours.

Fail closed, always. An unreachable JWKS endpoint is an authentication failure,
never a pass — "the identity provider was down so we let everyone in" is not a
degradation anybody wants.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient

# Clerk publishes JWKS at <issuer>/.well-known/jwks.json. The issuer is the
# instance's Frontend API origin — clerk init writes the publishable key from
# which it derives, and BLPL_CLERK_ISSUER states it outright.
_ISSUER_VAR = "BLPL_CLERK_ISSUER"
_JWKS_TTL_SECONDS = 3600
_LEEWAY_SECONDS = 10  # clock skew between this container and Clerk

_lock = threading.Lock()
_jwks_client: PyJWKClient | None = None
_jwks_expires_at = 0.0


class ClerkAuthError(RuntimeError):
    """The request carried no usable Clerk session. Always a 401 to the caller,
    and deliberately vague to it — the detail goes in the server log, because
    telling an unauthenticated caller *why* verification failed is free
    reconnaissance."""


class ClerkNotConfigured(RuntimeError):
    """No Clerk issuer is set. Distinct from a failed verification: nobody did
    anything wrong, the server is simply not set up yet, and the remedy is a
    config change rather than signing in again."""


@dataclass(frozen=True)
class ClerkUser:
    """The verified subject of a session token.

    ``id`` is Clerk's user id and is the only durable identity — email can
    change, and keying anything on it would let a user's rows be inherited by
    whoever claims the address next.
    """

    id: str
    email: str
    claims: dict


def issuer() -> str:
    return os.environ.get(_ISSUER_VAR, "").strip().rstrip("/")


def configured() -> bool:
    return bool(issuer())


def _client() -> PyJWKClient:
    """The JWKS client, refreshed hourly.

    Cached because it is consulted on every single request, and rebuilt on a TTL
    so Clerk rotating a signing key is picked up without a restart. PyJWKClient
    has its own key cache; this wrapper is about the *client* going stale, not
    the keys inside it.
    """
    global _jwks_client, _jwks_expires_at
    iss = issuer()
    if not iss:
        raise ClerkNotConfigured(f"{_ISSUER_VAR} is not set")
    with _lock:
        if _jwks_client is None or _jwks_expires_at <= time.time():
            _jwks_client = PyJWKClient(f"{iss}/.well-known/jwks.json", cache_keys=True)
            _jwks_expires_at = time.time() + _JWKS_TTL_SECONDS
        return _jwks_client


def verify(token: str) -> ClerkUser:
    """Verify a Clerk session token, or raise ClerkAuthError."""
    if not token:
        raise ClerkAuthError("no session token")
    iss = issuer()
    if not iss:
        raise ClerkNotConfigured(f"{_ISSUER_VAR} is not set")
    try:
        signing_key = _client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=iss,
            leeway=_LEEWAY_SECONDS,
            options={
                "require": ["exp", "iat", "sub"],
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iss": True,
                # No audience is configured on a default Clerk session token, and
                # demanding one would reject every real token. The issuer check
                # above is what ties the token to our instance.
                "verify_aud": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise ClerkAuthError(f"session token rejected: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — JWKS fetch failures land here
        # Fail closed: an unreachable identity provider is not a reason to admit
        # an unverified caller.
        raise ClerkAuthError(f"could not verify session token: {exc}") from exc

    subject = str(claims.get("sub") or "")
    if not subject:
        raise ClerkAuthError("session token carried no subject")
    return ClerkUser(id=subject, email=_email_from(claims), claims=claims)


def token_from_request(authorization: str | None, session_cookie: str | None) -> str:
    """Pull the token out of whichever place the client put it.

    Header first: it is the explicit choice a fetch makes, whereas the cookie
    rides along on every request including ones the page did not intend.
    """
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (session_cookie or "").strip()


def _email_from(claims: dict) -> str:
    """Clerk's session claims vary with how the instance's JWT template is set
    up — a default session token may carry no email at all. Try the usual spots
    and accept an empty string; the id is what identifies the user, and the
    email is only ever shown to the operator."""
    for key in ("email", "primary_email_address", "email_address"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return ""
