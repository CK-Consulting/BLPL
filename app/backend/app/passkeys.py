"""Unlocking with a passkey instead of a passphrase.

The second wrapping slot in ``userkey.py``, finally reachable. Both slots wrap
the *same* master key, so this adds a row and removes nothing: a lost passkey
falls back to the passphrase, and a forgotten passphrase falls back to a
passkey.

The shape is Cloudflare's: Clerk proves who you are, then a touch proves it is
still you at this keyboard and produces the key material. Two factors doing two
different jobs, rather than one prompt asked twice.

## Where the key actually comes from

Not from the signature. WebAuthn's assertion proves possession of a credential;
it produces no secret, which is exactly why an SSO login cannot open an
encrypted vault on its own. The secret comes from the **PRF extension**: the
authenticator evaluates a pseudo-random function over a stored salt and returns
32 bytes that are stable for that credential, that salt and that relying party —
and that exist nowhere until the user touches the key.

So the chain is:

    touch ──▶ authenticator ──▶ PRF(salt) ──HKDF──▶ KEK ──unwrap──▶ master key

The salt is stored here in the clear. It is a selector, not a secret; what makes
the output unguessable is the authenticator's key, which never leaves it.

## The honest part: the PRF output crosses the wire

The browser sends those 32 bytes to this server, which derives the KEK and
unwraps. That is the same trust model the passphrase already has — it is typed
into the same page and posted to the same endpoint — and for the same
unavoidable reason: stage runs are server-side subprocesses that need the master
key to inject into a child environment. A design where the browser held the key
alone would be strictly better and cannot run KiCad.

What the passkey does improve, concretely:

* The secret is 32 uniform bytes from hardware rather than something a human
  chose and can reuse on another site.
* It is bound to this relying party by the authenticator, so a phishing origin
  cannot obtain it — the browser will not even evaluate the PRF for a caller
  that does not match the RP ID.
* It cannot be shoulder-surfed, keylogged, or typed into the wrong window.

What it does not improve: an operator of a running server still sees the key in
memory for the length of the session. Nothing at this layer changes that.

## Verifying the assertion at all

Strictly, the AES-GCM tag is what protects the master key: a forged assertion
without the authenticator yields no PRF output, so the unwrap simply fails. The
signature is still verified, against a stored public key, because it costs one
library call and it stops this endpoint being a free oracle — a challenge that
is checked and burned is what keeps a captured request from being replayed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .models import User, UserKeyCredential
from .userkey import Wrapped, unwrap_with_prf, wrap_with_prf
from .vault import VaultError

logger = logging.getLogger("blpl.passkeys")

RP_NAME = "BLPL"
_SALT_LEN = 32

# A ceremony a user walks away from must not stay valid. Comfortably longer than
# the 60s the browser gives its own prompt, so the failure a user sees is the
# authenticator timing out — one clear message rather than a race between two.
_CHALLENGE_TTL = 180.0


class PasskeyError(VaultError):
    """A ceremony could not be completed."""


class NoPasskeys(PasskeyError):
    """This account has no credential to authenticate with."""


# --------------------------------------------------------------- relying party


@dataclass(frozen=True)
class RelyingParty:
    rp_id: str
    origins: list[str]


def relying_party(origin_header: str = "") -> RelyingParty:
    """Which domain these credentials belong to.

    Configured explicitly in production, because the RP ID is what a credential
    is *bound to*: change it and every existing passkey stops matching, which
    presents as "my key stopped working" with no clue as to why.

    The fallback derives it from the caller's Origin so a developer on
    localhost, or a fresh tunnel hostname, works without configuration. That is
    defence-in-depth being dropped, not the primary control — an authenticator
    refuses to evaluate the PRF for a caller whose origin does not match the RP
    ID it was created for, so a phishing site cannot obtain the key material
    either way. It is logged because a production server running on the fallback
    is a configuration mistake, not a choice.
    """
    rp_id = os.environ.get("BLPL_WEBAUTHN_RP_ID", "").strip()
    origins = [
        o.strip() for o in os.environ.get("BLPL_WEBAUTHN_ORIGIN", "").split(",") if o.strip()
    ]
    if rp_id and origins:
        return RelyingParty(rp_id, origins)

    if not origin_header:
        raise PasskeyError(
            "passkeys need BLPL_WEBAUTHN_RP_ID and BLPL_WEBAUTHN_ORIGIN set, or a "
            "browser Origin to infer them from"
        )
    host = urlparse(origin_header).hostname or ""
    if not host:
        raise PasskeyError(f"could not read a hostname from origin {origin_header!r}")
    logger.warning(
        "BLPL_WEBAUTHN_RP_ID is not set; inferring %r from the request origin. Set it "
        "explicitly in production — the RP ID is what existing passkeys are bound to.",
        host,
    )
    return RelyingParty(rp_id or host, origins or [origin_header])


# ------------------------------------------------------------ challenge store


@dataclass
class _Pending:
    challenge: bytes
    expires_at: float
    # Carried from begin to finish so the salt that ends up in the row is the
    # one the authenticator actually evaluated. Regenerating it at finish would
    # store a salt that unwraps nothing — and would do so silently, since the
    # wrap succeeds and only the *next* unlock fails.
    salt: bytes | None = None


class Challenges:
    """Outstanding ceremonies, in memory.

    In process for the same reason ``unlock.Unlocked`` is: the API runs a single
    worker, and a challenge is worthless the moment it is used. A restart
    invalidates every ceremony in flight, which costs a user one retry.

    Burned on use. A challenge that can be presented twice is a replay, and the
    signature check would happily pass the second time.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    def issue(self, key: str, challenge: bytes, salt: bytes | None = None) -> None:
        with self._lock:
            self._pending[key] = _Pending(challenge, time.time() + _CHALLENGE_TTL, salt)

    def take(self, key: str) -> _Pending:
        """The pending ceremony, removed. Raises if there is not one."""
        with self._lock:
            pending = self._pending.pop(key, None)
        if pending is None:
            raise PasskeyError("no ceremony in progress; start again")
        if pending.expires_at < time.time():
            raise PasskeyError("that took too long; start again")
        return pending


# ------------------------------------------------------------------ ceremonies


def registration_options(user: User, existing: list[UserKeyCredential], rp: RelyingParty) -> tuple[dict, bytes]:
    """Options for creating a passkey, and the challenge to remember.

    Returns the options as a dict so the PRF extension can be added: it is a
    *client* extension — the browser and authenticator handle it and the result
    never reaches a server — so py_webauthn does not model it, and the server's
    only job is to ask for it and to store the salt.
    """
    salt = os.urandom(_SALT_LEN)
    options = generate_registration_options(
        rp_id=rp.rp_id,
        rp_name=RP_NAME,
        # Stable per user and not the email: a user id that is also a login
        # identifier ends up displayed by password managers and synced between
        # devices, and changing an email should not orphan a credential.
        user_id=str(user.id).encode(),
        user_name=user.email or f"user-{user.id}",
        user_display_name=user.email or f"BLPL user {user.id}",
        # Refusing a key the account already has is what turns "add a passkey"
        # into an error the user can act on, rather than a silent second row for
        # the same authenticator.
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in existing
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            # Discoverable, so unlocking can offer the key without being told
            # which account it belongs to first.
            resident_key=ResidentKeyRequirement.PREFERRED,
            # Required, not preferred: this credential stands in for a
            # passphrase, so a touch that proves presence without proving it is
            # *this person* would be a downgrade of the thing it replaces.
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    body = json.loads(options_to_json(options))
    body["extensions"] = {
        # Empty object asks whether PRF is available at all; eval asks for the
        # output at the same time. Browsers that support the second answer in
        # one ceremony, and those that do not fall back to a second one — which
        # is why the client checks for results rather than assuming them.
        "prf": {"eval": {"first": bytes_to_base64url(salt)}},
    }
    return body, base64url_to_bytes(body["challenge"])


def register(
    session: Session,
    user: User,
    credential: dict,
    prf_output: bytes,
    master_key: bytes,
    challenge: bytes,
    salt: bytes,
    rp: RelyingParty,
    label: str = "",
) -> UserKeyCredential:
    """Verify a new credential and wrap the master key under its PRF output."""
    if len(prf_output) < 32:
        # Without PRF the credential could authenticate but never unwrap
        # anything, so registering it would produce a passkey that signs in and
        # then cannot open a single project. Refused here rather than stored.
        raise PasskeyError(
            "this authenticator did not provide a PRF secret, so it cannot unlock "
            "your data — use a security key or platform authenticator that supports it"
        )
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origins,
            require_user_verification=True,
        )
    except Exception as exc:  # py_webauthn raises a family of these
        raise PasskeyError(f"that passkey could not be verified: {exc}") from exc

    credential_id = bytes_to_base64url(verified.credential_id)
    if session.scalar(
        select(UserKeyCredential).where(UserKeyCredential.credential_id == credential_id)
    ):
        raise PasskeyError("that passkey is already registered")

    wrapped = wrap_with_prf(prf_output, master_key)
    row = UserKeyCredential(
        user_id=user.id,
        label=label.strip()[:128],
        credential_id=credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        prf_salt=salt,
        nonce=wrapped.nonce,
        ciphertext=wrapped.ciphertext,
    )
    session.add(row)
    session.flush()
    return row


def authentication_options(credentials: list[UserKeyCredential], rp: RelyingParty) -> tuple[dict, bytes]:
    """Options for unlocking, asking each credential for its own PRF salt."""
    if not credentials:
        raise NoPasskeys("this account has no passkeys")

    options = generate_authentication_options(
        rp_id=rp.rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id))
            for c in credentials
        ],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    body = json.loads(options_to_json(options))
    # evalByCredential rather than eval, because each credential has its own
    # salt: the browser picks the entry matching whichever key the user actually
    # touches. A single shared salt would work today and would be wrong the
    # moment a second key is registered.
    body["extensions"] = {
        "prf": {
            "evalByCredential": {
                c.credential_id: {"first": bytes_to_base64url(c.prf_salt)} for c in credentials
            }
        }
    }
    return body, base64url_to_bytes(body["challenge"])


def authenticate(
    session: Session,
    user: User,
    credential: dict,
    prf_output: bytes,
    challenge: bytes,
    rp: RelyingParty,
) -> bytes:
    """Verify an assertion and return the master key it unwraps."""
    raw_id = credential.get("id") or credential.get("rawId") or ""
    row = session.scalar(
        select(UserKeyCredential).where(
            UserKeyCredential.credential_id == raw_id,
            # Scoped to the signed-in user, so a valid assertion for somebody
            # else's credential cannot unwrap anything here.
            UserKeyCredential.user_id == user.id,
        )
    )
    if row is None:
        raise PasskeyError("that passkey is not registered to this account")

    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.rp_id,
            expected_origin=rp.origins,
            credential_public_key=row.public_key,
            credential_current_sign_count=row.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        raise PasskeyError(f"that passkey could not be verified: {exc}") from exc

    # Many platform authenticators report zero forever, which is legitimate and
    # means the counter cannot detect cloning for them. Recording what comes
    # back keeps the check meaningful for the authenticators that do implement
    # it — py_webauthn has already rejected a counter that went backwards.
    row.sign_count = verified.new_sign_count
    session.flush()

    return unwrap_with_prf(prf_output, Wrapped(nonce=row.nonce, ciphertext=row.ciphertext))


def list_for(session: Session, user: User) -> list[UserKeyCredential]:
    return list(
        session.scalars(
            select(UserKeyCredential)
            .where(UserKeyCredential.user_id == user.id)
            .order_by(UserKeyCredential.created_at)
        )
    )


def forget(session: Session, user: User, credential_row_id: int) -> bool:
    """Remove a passkey.

    Removing the last one is allowed: the passphrase slot always exists, so
    there is no state where a user can delete their way out of their own data.
    That is the property the two-slot design was built for.
    """
    row = session.scalar(
        select(UserKeyCredential).where(
            UserKeyCredential.id == credential_row_id,
            UserKeyCredential.user_id == user.id,
        )
    )
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True
