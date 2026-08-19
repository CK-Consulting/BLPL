"""Unlocking with a passkey instead of typing a passphrase.

The ceremonies are exercised against a real software authenticator
(`webauthn_sim.py`) rather than a stubbed verifier, because the thing worth
testing *is* the verification: a test that patches `verify_authentication_response`
to return success proves only that a function told to succeed succeeded.

Two properties are load-bearing and get their own tests:

* Both slots open the same master key, so a passkey and a passphrase are
  interchangeable and neither can lock you out of the other.
* The PRF output is what actually decrypts. The signature proves the credential
  is genuine and produces no key material — which is precisely why an SSO login
  cannot open an encrypted vault on its own.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

# The whole file is about WebAuthn, so skipping it wholesale is honest. Without
# this an absent optional dep fails at *collection*, which aborts the run and
# takes every other test in the suite with it.
pytest.importorskip("webauthn")

from webauthn.helpers import bytes_to_base64url  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app" / "backend"))

from webauthn_sim import SoftwareAuthenticator  # noqa: E402

RP_ID = "testserver"
ORIGIN = "https://testserver"


@pytest.fixture(autouse=True)
def _relying_party(monkeypatch):
    """Pin the RP explicitly, as a deployment should. Without it the code infers
    one from the request origin, which is a documented dev-only fallback and not
    what these tests are about."""
    monkeypatch.setenv("BLPL_WEBAUTHN_RP_ID", RP_ID)
    monkeypatch.setenv("BLPL_WEBAUTHN_ORIGIN", ORIGIN)


def _headers():
    return {"Origin": ORIGIN}


def _enrol(client, label="My laptop", authenticator=None):
    """Add a passkey the way the browser does: begin, create, finish."""
    options = client.post(
        "/api/passkeys/register/begin", json={"label": label}, headers=_headers()
    ).json()
    challenge = _b64(options["challenge"])
    auth = authenticator or SoftwareAuthenticator(RP_ID)
    return (
        auth,
        client.post(
            "/api/passkeys/register/finish",
            json={
                "credential": auth.create(challenge, ORIGIN),
                "prf_output": bytes_to_base64url(auth.prf_output),
                "label": label,
            },
            headers=_headers(),
        ),
    )


def _unlock_with(client, auth, prf_output=None):
    options = client.post("/api/passkeys/auth/begin", headers=_headers()).json()
    challenge = _b64(options["challenge"])
    return client.post(
        "/api/passkeys/auth/finish",
        json={
            "credential": auth.get(challenge, ORIGIN),
            "prf_output": bytes_to_base64url(
                auth.prf_output if prf_output is None else prf_output
            ),
        },
        headers=_headers(),
    )


def _b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# ------------------------------------------------------------------ enrolment


def test_enrolling_then_unlocking_with_it(unlocked):
    auth, created = _enrol(unlocked)
    assert created.status_code == 200

    unlocked.post("/api/auth/lock")
    assert unlocked.post("/api/projects/init", json={"name": "while-locked"}).status_code == 423

    assert _unlock_with(unlocked, auth).json() == {"unlocked": True}
    # And the session really is open — a route that needs the master key works.
    assert unlocked.post("/api/projects/init", json={"name": "after"}).status_code == 200


def test_enrolment_needs_an_unlocked_session(client):
    """The credential wraps the master key, so adding one requires already being
    able to produce it. Signing in with Clerk alone must not be enough."""
    from conftest import sign_in_only

    signed_in = sign_in_only(client)
    r = signed_in.post("/api/passkeys/register/begin", json={}, headers=_headers())
    assert r.status_code in (423, 428)


def test_a_passkey_without_prf_is_refused(unlocked):
    """It could sign in and never decrypt anything — a key that authenticates
    and then cannot open a single project is worse than no key."""
    options = unlocked.post("/api/passkeys/register/begin", json={}, headers=_headers()).json()
    auth = SoftwareAuthenticator(RP_ID)
    r = unlocked.post(
        "/api/passkeys/register/finish",
        json={
            "credential": auth.create(_b64(options["challenge"]), ORIGIN),
            "prf_output": "",
        },
        headers=_headers(),
    )
    assert r.status_code == 400
    assert "PRF secret" in r.json()["detail"]
    assert unlocked.get("/api/passkeys").json() == []


def test_the_same_passkey_cannot_be_registered_twice(unlocked):
    auth, _ = _enrol(unlocked)
    _, second = _enrol(unlocked, authenticator=auth)
    assert second.status_code == 400
    assert "already registered" in second.json()["detail"]


# --------------------------------------------------------------- verification


def test_a_replayed_ceremony_is_refused(unlocked):
    """The challenge is burned on use. Capturing one exchange and sending it
    again must not unlock anything."""
    auth, _ = _enrol(unlocked)
    options = unlocked.post("/api/passkeys/auth/begin", headers=_headers()).json()
    assertion = auth.get(_b64(options["challenge"]), ORIGIN)
    payload = {"credential": assertion, "prf_output": bytes_to_base64url(auth.prf_output)}

    first = unlocked.post("/api/passkeys/auth/finish", json=payload, headers=_headers())
    assert first.status_code == 200
    replayed = unlocked.post("/api/passkeys/auth/finish", json=payload, headers=_headers())
    assert replayed.status_code == 401
    assert "no ceremony in progress" in replayed.json()["detail"]


def test_an_assertion_from_a_different_key_is_refused(unlocked):
    """The signature is checked against the public key stored at registration —
    a well-formed assertion from another authenticator is still a forgery."""
    auth, _ = _enrol(unlocked)
    impostor = SoftwareAuthenticator(RP_ID, credential_id=auth.credential_id)
    impostor.prf_output = auth.prf_output  # even knowing the secret

    r = _unlock_with(unlocked, impostor)
    assert r.status_code == 401
    assert "could not be verified" in r.json()["detail"]


def test_an_assertion_for_another_origin_is_refused(unlocked):
    auth, _ = _enrol(unlocked)
    options = unlocked.post("/api/passkeys/auth/begin", headers=_headers()).json()
    r = unlocked.post(
        "/api/passkeys/auth/finish",
        json={
            "credential": auth.get(_b64(options["challenge"]), "https://evil.example"),
            "prf_output": bytes_to_base64url(auth.prf_output),
        },
        headers=_headers(),
    )
    assert r.status_code == 401


def test_a_valid_signature_with_the_wrong_prf_does_not_unlock(unlocked):
    """The point of the whole design, in one test.

    The assertion here is genuine and verifies — and it still yields nothing,
    because the signature proves possession and produces no key material. The
    secret comes from the PRF, and AES-GCM's tag is what actually refuses.
    """
    auth, _ = _enrol(unlocked)
    unlocked.post("/api/auth/lock")

    r = _unlock_with(unlocked, auth, prf_output=b"\x00" * 32)
    assert r.status_code == 401
    assert "did not unwrap" in r.json()["detail"]

    # Still locked: a failed attempt must not leave a half-open session.
    assert unlocked.post("/api/projects/init", json={"name": "nope"}).status_code == 423


def test_one_persons_passkey_cannot_unlock_another_account(unlocked, second_user):
    auth, _ = _enrol(unlocked)
    r = second_user.post("/api/passkeys/auth/begin", headers=_headers())
    assert r.status_code == 404  # they have none of their own

    # And presenting the first user's credential to the second account is
    # refused before it can unwrap anything — the lookup is scoped to the
    # signed-in user, so a genuine assertion for somebody else's credential
    # finds no row here.
    presented = second_user.post("/api/passkeys/auth/finish", json={
        "credential": auth.get(b"x" * 32, ORIGIN),
        "prf_output": bytes_to_base64url(auth.prf_output),
    }, headers=_headers())
    assert presented.status_code == 401


# ------------------------------------------------------------- the two slots


def test_both_slots_open_the_same_master_key(unlocked):
    """A passkey and a passphrase are two doors to one key, not two keys. If
    they diverged, data written after enrolling a passkey would be unreadable
    with the passphrase — which is how people lose everything."""
    auth, _ = _enrol(unlocked)
    unlocked.post("/api/projects/init", json={"name": "shared"})
    unlocked.put("/api/projects/shared/files/a.md", json={"content": "written\n"})

    # Locking seals the workspace, so each slot has to reopen it as well —
    # which makes this the stronger assertion: the *project* key is reachable
    # through either door, not just the master key.
    unlocked.post("/api/auth/lock")
    _unlock_with(unlocked, auth)
    assert unlocked.post("/api/projects/shared/open").status_code == 200
    assert unlocked.get("/api/projects/shared/files/a.md").json()["content"] == "written\n"

    unlocked.post("/api/auth/lock")
    from conftest import PASSPHRASE

    unlocked.post("/api/auth/unlock", json={"passphrase": PASSPHRASE})
    assert unlocked.post("/api/projects/shared/open").status_code == 200
    assert unlocked.get("/api/projects/shared/files/a.md").json()["content"] == "written\n"


def test_removing_the_last_passkey_is_allowed(unlocked):
    """The passphrase slot always exists, so nobody can delete their way out of
    their own data — the property the two-slot design was built for."""
    _enrol(unlocked)
    row = unlocked.get("/api/passkeys").json()[0]

    assert unlocked.delete(f"/api/passkeys/{row['id']}").status_code == 200
    assert unlocked.get("/api/passkeys").json() == []

    unlocked.post("/api/auth/lock")
    from conftest import PASSPHRASE

    assert unlocked.post("/api/auth/unlock", json={"passphrase": PASSPHRASE}).json() == {
        "unlocked": True
    }


def test_you_cannot_delete_someone_elses_passkey(unlocked, second_user):
    _enrol(unlocked)
    row = unlocked.get("/api/passkeys").json()[0]
    assert second_user.delete(f"/api/passkeys/{row['id']}").status_code == 404


def test_unlocking_with_a_passkey_does_the_same_backfills(unlocked):
    """Two doors into one session, so both must leave it in the same state. A
    passkey unlock that skipped ensure_keypair would leave an account that
    cannot be shared with — surfacing much later, as somebody else's error."""
    auth, _ = _enrol(unlocked)
    unlocked.post("/api/auth/lock")
    _unlock_with(unlocked, auth)

    # A keypair is what sharing seals project keys to.
    assert unlocked.post("/api/projects/init", json={"name": "p"}).status_code == 200
    r = unlocked.post("/api/projects/p/members", json={"email": "other@example.com"})
    assert r.status_code in (200, 404)  # 404 only if the invitee has no account yet
