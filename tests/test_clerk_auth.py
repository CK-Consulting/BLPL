"""Verifying a Clerk session token, and refusing everything else.

This is the whole gate. Every route under /api except the health and config
probes is behind it, so a hole here is a hole in all of them — and unlike most
bugs, a permissive one is invisible: everything keeps working, for everybody,
including people who should not be there.

The case that matters most is the third one. Anyone can create a Clerk instance
in a minute, and a token it signs is a perfectly valid JWT with a real signature
from a real JWKS. Only the issuer check distinguishes it from ours, and that
check is the kind a reasonable person omits while getting a login working.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from app import clerk_auth  # noqa: E402

_OURS = "https://immense-llama-88.clerk.accounts.dev"


@pytest.fixture
def signer(monkeypatch):
    """A local RSA key standing in for Clerk's, so no test needs the network."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    class _Key:
        pass

    signing = _Key()
    signing.key = key.public_key()

    class _Client:
        def get_signing_key_from_jwt(self, token):
            return signing

    monkeypatch.setenv("BLPL_CLERK_ISSUER", _OURS)
    monkeypatch.setattr(clerk_auth, "_client", lambda: _Client())

    def make(**claims):
        payload = {
            "iss": _OURS,
            "sub": "user_abc",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
        payload.update(claims)
        return jwt.encode(payload, pem, algorithm="RS256")

    make.pem = pem
    return make


def test_a_valid_token_resolves_to_its_subject(signer):
    who = clerk_auth.verify(signer())
    assert who.id == "user_abc"


def test_a_token_from_another_clerk_instance_is_refused(signer):
    """The one that matters. Anyone can stand up a Clerk instance; its tokens are
    properly signed and verify against a real JWKS. Without the issuer check this
    is indistinguishable from one of ours, and it is the check most easily left
    out while getting sign-in working."""
    with pytest.raises(clerk_auth.ClerkAuthError):
        clerk_auth.verify(signer(iss="https://attacker.clerk.accounts.dev"))


def test_an_unsigned_token_is_refused(signer):
    """alg=none: the classic. A JWT library asked to decode without demanding an
    algorithm will happily accept a token that nobody signed."""
    forged = jwt.encode({"iss": _OURS, "sub": "user_evil", "iat": int(time.time()),
                         "exp": int(time.time()) + 60}, key=None, algorithm="none")
    with pytest.raises(clerk_auth.ClerkAuthError):
        clerk_auth.verify(forged)


def test_an_expired_token_is_refused(signer):
    with pytest.raises(clerk_auth.ClerkAuthError):
        clerk_auth.verify(signer(exp=int(time.time()) - 60))


def test_a_token_without_a_subject_is_refused(signer):
    """Nothing to attach keys or projects to; admitting it would create a user
    row keyed on the empty string, shared by everyone who managed it."""
    with pytest.raises(clerk_auth.ClerkAuthError):
        clerk_auth.verify(signer(sub=""))


@pytest.mark.parametrize("token", ["", "   ", "not-a-jwt", "a.b.c"])
def test_garbage_is_refused(signer, token):
    with pytest.raises(clerk_auth.ClerkAuthError):
        clerk_auth.verify(token)


def test_an_unreachable_jwks_fails_closed(monkeypatch):
    """"The identity provider was down, so we let everyone in" is not a
    degradation anyone wants. It has to be an authentication failure."""
    monkeypatch.setenv("BLPL_CLERK_ISSUER", _OURS)

    def explode():
        raise OSError("connection refused")

    monkeypatch.setattr(clerk_auth, "_client", explode)
    with pytest.raises(clerk_auth.ClerkAuthError):
        clerk_auth.verify("a.b.c")


def test_no_issuer_configured_is_not_an_auth_failure(monkeypatch):
    """Distinct from a rejected token, because the remedy is completely
    different: nobody can fix this by signing in again."""
    monkeypatch.delenv("BLPL_CLERK_ISSUER", raising=False)
    assert clerk_auth.configured() is False
    with pytest.raises(clerk_auth.ClerkNotConfigured):
        clerk_auth.verify("anything")


def test_the_header_is_preferred_over_the_cookie():
    """The header is what a fetch deliberately sends; the cookie rides along on
    every request whether the page meant it or not."""
    assert clerk_auth.token_from_request("Bearer hdr", "cookie") == "hdr"
    assert clerk_auth.token_from_request(None, "cookie") == "cookie"
    assert clerk_auth.token_from_request("Basic xyz", "cookie") == "cookie"
    assert clerk_auth.token_from_request(None, None) == ""
